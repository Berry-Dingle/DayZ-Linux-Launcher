from __future__ import annotations

import hashlib
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import companion_restart_phase2_detection as detection_module
from .companion_restart_phase2_authority import (
    AuthorityDecision,
    RegimeState,
    evaluate_authority,
)
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
from .companion_restart_phase2_continuity import (
    CONTINUITY_PROVENANCE_VERSION,
    ContinuityShadowSnapshot,
    ContinuityShadowTracker,
    build_cadence_streaks,
    build_continuity_chains,
    continuity_span_terminates_chain,
    deterministic_continuity_chain_id,
    extract_interval_relationships,
    observed_transition_spans_from_events,
    reconstruct_historical_event_provenance,
    span_from_observations,
)
from .companion_restart_phase2_expected_windows import (
    ExpectedWindowEpisodeEvidence,
    ExpectedWindowLedgerRecord,
    ExpectedWindowOutcome,
    ExpectedWindowRevision,
    classify_expected_window,
    continuity_spans_from_coverage,
    deterministic_model_revision_id,
    reconcile_expected_window_ledger,
)
from .a2s_status_diagnostics import result_classification
from .companion_restart_phase2_scoring import (
    AGGREGATE_SEMANTICS_VERSION,
    CANDIDATE_PERIODS,
    SCORING_SEMANTICS_VERSION,
    CandidateAggregate,
    CoveredExpectedMiss,
    CoverageKind,
    CoverageSegment,
    CoverageTimeline,
    GATES,
    LongTermAggregate,
    PhaseRecencyPolicy,
    RegimeSummary,
    RegimeStatus,
    RestartScheduleScorer,
    ScheduleScore,
    candidate_phase_tolerance,
)
from .companion_restart_phase2_storage import (
    PHASE2_SCHEMA_VERSION,
    PHASE2_SCORING_ALGORITHM_VERSION,
    Phase2InitializationStatus,
    Phase2MigrationResult,
    atomic_write_phase2_state,
    initialize_phase2_state,
    normalize_phase2_state,
    new_phase2_state,
)


RUNTIME_STATE_VERSION = 1
ACTIVE_EPISODE_SNAPSHOT_VERSION = 1
MAX_FINALIZED_EVENTS = 80
MAX_COVERAGE_SEGMENTS = 120
MAX_FIRED_KEYS = 200
MAX_ROUTED_FINGERPRINTS = 160
MAX_INCOMPLETE_EPISODES = 30
MAX_MONITORING_SESSIONS = 120
PLAYER_OBSERVATION_RETENTION_SECONDS = 40 * 60.0
PERSIST_HEARTBEAT_SECONDS = 60.0
COVERAGE_MAX_AGE_SECONDS = 60 * 24 * 3600
PERSISTED_FUTURE_SKEW_SECONDS = 5 * 60
RECOVERY_ALERT_EXPECTED_WINDOW_SECONDS = 10 * 60.0
RECOVERY_ALERT_OUT_OF_WINDOW_HOLD_SECONDS = 45.0
MAX_FULL_DETAIL_SERVERS = 32
MAX_COMPACT_DETAIL_SERVERS = 250
COMPACT_EVENT_LIMIT = 12
COLD_EVENT_LIMIT = 2


logger = logging.getLogger(__name__)


# Preserve the existing runtime patch/test seam while making every production
# runtime save pass through the schema-aware mixed-version guard.
def atomic_write_json(path: str | Path, state: object) -> None:
    atomic_write_phase2_state(path, state)


def _startup_failure_kind(exc: BaseException) -> str:
    current = exc
    seen: set[int] = set()
    while (
        getattr(current, "__cause__", None) is not None
        and id(current) not in seen
    ):
        seen.add(id(current))
        current = current.__cause__  # type: ignore[assignment]
    if isinstance(current, PermissionError):
        return "permission"
    if isinstance(current, OSError):
        return "io"
    names = {type(exc).__name__, type(current).__name__}
    if "Schema4WriterLockError" in names:
        return "writer_lock"
    if "Schema4VersionError" in names:
        return "schema_version"
    if "Schema4ValidationError" in names:
        return "validation"
    if "Schema4RuntimeError" in names:
        return "schema4_runtime"
    return type(current).__name__


class RuntimePersistenceStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED_INITIALIZATION_FAILED = "disabled_initialization_failed"
    DISABLED_WRITE_FAILED = "disabled_write_failed"


class LiveResultDisposition(str, Enum):
    HEALTHY = "healthy"
    NEUTRAL = "neutral"
    QUALIFYING_FAILURE = "qualifying_failure"
    PROTOCOL_FAILURE = "protocol_failure"


class RecoveryAlertWindowStatus(str, Enum):
    NO_SAFE_EXPECTATION = "no_safe_expectation"
    INSIDE_WINDOW = "inside_window"
    OUTSIDE_WINDOW = "outside_window"


@dataclass(frozen=True)
class RecoveryAlertExpectedWindow:
    status: RecoveryAlertWindowStatus
    event_id: str | None = None
    restart_started_at: float | None = None
    candidate_period_seconds: int | None = None
    nearest_expected_restart_at: float | None = None
    residual_seconds: float | None = None


def classify_recovery_alert_expected_window(
    *,
    event_id: str | None,
    restart_started_at: float | None,
    safe_prediction: bool,
    candidate_period_seconds: int | None,
    predicted_occurrence_at: float | None,
) -> RecoveryAlertExpectedWindow:
    """Classify one provisional restart start against a safe phase lattice."""

    base = RecoveryAlertExpectedWindow(
        status=RecoveryAlertWindowStatus.NO_SAFE_EXPECTATION,
        event_id=str(event_id) if event_id else None,
        restart_started_at=restart_started_at,
    )
    if not safe_prediction or not event_id:
        return base
    if (
        restart_started_at is None
        or not math.isfinite(restart_started_at)
        or restart_started_at < 0
        or type(candidate_period_seconds) is not int
        or candidate_period_seconds <= 0
        or predicted_occurrence_at is None
        or not math.isfinite(predicted_occurrence_at)
        or predicted_occurrence_at < 0
    ):
        return base
    period = candidate_period_seconds
    cycle = math.floor((restart_started_at - predicted_occurrence_at) / period)
    before = predicted_occurrence_at + cycle * period
    after = before + period
    nearest = min((before, after), key=lambda value: abs(restart_started_at - value))
    residual = abs(restart_started_at - nearest)
    return RecoveryAlertExpectedWindow(
        status=(
            RecoveryAlertWindowStatus.INSIDE_WINDOW
            if residual <= RECOVERY_ALERT_EXPECTED_WINDOW_SECONDS
            else RecoveryAlertWindowStatus.OUTSIDE_WINDOW
        ),
        event_id=str(event_id),
        restart_started_at=float(restart_started_at),
        candidate_period_seconds=period,
        nearest_expected_restart_at=float(nearest),
        residual_seconds=float(residual),
    )


@dataclass(frozen=True)
class RuntimeUpdate:
    accepted: bool
    finalized_events: tuple[PhysicalRestartEvent, ...] = ()
    query_visible_repopulation_event_id: str | None = None
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


@dataclass(frozen=True)
class AuthorityConsumerActionResult:
    attempted: bool
    emitted: bool
    source: str
    key: str | None
    persistence_generation: int | None
    reason_codes: tuple[str, ...]


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
    continuity_chain_id: str = ""
    dirty: bool = False
    last_saved_at: float = 0.0
    last_expected_miss_signature: tuple[tuple[int, int], ...] = ()
    incomplete_episodes: list[dict] | None = None
    extra_fields: dict = field(default_factory=dict)
    expected_misses: list[CoveredExpectedMiss] = field(default_factory=list)
    monitoring_sessions: list[dict] = field(default_factory=list)
    # Deliberately session-local: schema-4 persists physical evidence, while raw
    # occupancy samples only decide whether a live query-visible miss was observable.
    player_observations: list[tuple[float, int]] = field(default_factory=list)

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
        continuity_shadow_enabled: bool = False,
        expected_window_v2_shadow_enabled: bool = False,
        continuity_authority_shadow_enabled: bool = False,
        schema4_authority_shadow_enabled: bool = False,
        schema4_authority_shadow_state: Mapping[str, object] | None = None,
        authoritative_schema4_runtime_enabled: bool = False,
        schema4_authority_consumer_shadow_enabled: bool = False,
        schema4_authority_production_cutover_enabled: bool = False,
        _authoritative_schema4_backend: object | None = None,
        _schema4_startup_preparation: object | None = None,
    ) -> None:
        self.migration = migration
        self.active_path = Path(migration.active_path)
        self.state = normalize_phase2_state(migration.state)
        self.app_session_id = app_session_id or str(uuid.uuid4())
        self.detection_config = detection_config or DetectionConfig()
        self.scorer = RestartScheduleScorer()
        self.continuity_shadow_enabled = bool(continuity_shadow_enabled)
        self._continuity_shadow_trackers: dict[str, ContinuityShadowTracker] = {}
        self._continuity_shadow_logged_ids: set[str] = set()
        self.expected_window_v2_shadow_enabled = bool(
            expected_window_v2_shadow_enabled
        )
        self._expected_window_v2_shadow_ledgers: dict[
            str, list[ExpectedWindowLedgerRecord]
        ] = {}
        self._expected_window_v2_shadow_logged_ids: set[str] = set()
        self.continuity_authority_shadow_enabled = bool(
            continuity_authority_shadow_enabled
        )
        self._continuity_authority_shadow_decisions: dict[str, AuthorityDecision] = {}
        self._continuity_authority_shadow_logged_ids: set[str] = set()
        self.schema4_authority_shadow_enabled = bool(
            schema4_authority_shadow_enabled
        )
        self._schema4_authority_shadow_state = schema4_authority_shadow_state
        self._schema4_authority_shadow_comparisons: dict[str, object] = {}
        self._schema4_authority_shadow_logged_ids: set[str] = set()
        self.authoritative_schema4_runtime_enabled = bool(
            authoritative_schema4_runtime_enabled
        )
        self._authoritative_schema4_backend = _authoritative_schema4_backend
        self.schema4_startup_preparation = _schema4_startup_preparation
        self.schema4_authority_consumer_shadow_enabled = bool(
            schema4_authority_consumer_shadow_enabled
        )
        self.schema4_authority_production_cutover_enabled = bool(
            schema4_authority_production_cutover_enabled
        )
        self._authority_consumer_shadow_decisions: dict[str, object] = {}
        self._authority_consumer_comparisons: dict[str, object] = {}
        self._authority_consumer_resolutions: dict[str, object] = {}
        self._authority_consumer_logged_ids: set[str] = set()
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
        continuity_shadow_enabled: bool = False,
        expected_window_v2_shadow_enabled: bool = False,
        continuity_authority_shadow_enabled: bool = False,
        schema4_authority_shadow_enabled: bool = False,
        schema4_authority_shadow_state: Mapping[str, object] | None = None,
        authoritative_schema4_runtime_enabled: bool = False,
        schema4_authority_consumer_shadow_enabled: bool = False,
        schema4_authority_production_cutover_enabled: bool = False,
    ) -> "Phase2RestartRuntime":
        schema4_backend = None
        schema4_startup_preparation = None
        if authoritative_schema4_runtime_enabled:
            from .companion_restart_phase2_schema4 import (
                Schema4StartupPreparationStatus,
                prepare_schema4_state_for_startup,
            )
            from .companion_restart_phase2_schema4_runtime import (
                AuthoritativeSchema4Runtime,
            )

            schema4_startup_preparation = prepare_schema4_state_for_startup(
                active_path=active_path,
                legacy_path=legacy_path,
                now=now,
                generation_id=generation_id,
            )
            schema4_backend = AuthoritativeSchema4Runtime.open(
                active_path, enabled=True
            )
            fresh_created = (
                schema4_startup_preparation.status
                is Schema4StartupPreparationStatus.FRESH_CREATED
            )
            migration = Phase2MigrationResult(
                state=schema4_backend.schema3_projection(),
                status=(
                    Phase2InitializationStatus.FRESH_CREATED
                    if fresh_created
                    else Phase2InitializationStatus.ACTIVE_LOADED
                ),
                reset_performed=bool(
                    schema4_startup_preparation.legacy_reset_performed
                ),
                recovery_performed=False,
                active_path=Path(active_path),
                backup_path=(
                    schema4_startup_preparation.legacy_backup_path
                    or schema4_startup_preparation.backup_path
                ),
                legacy_checksum=None,
                persistence_enabled=True,
            )
        else:
            migration = initialize_phase2_state(
                active_path=active_path,
                legacy_path=legacy_path,
                now=now,
                generation_id=generation_id,
            )
        try:
            return cls(
                migration,
                now=now,
                app_session_id=app_session_id,
                detection_config=detection_config,
                continuity_shadow_enabled=continuity_shadow_enabled,
                expected_window_v2_shadow_enabled=expected_window_v2_shadow_enabled,
                continuity_authority_shadow_enabled=continuity_authority_shadow_enabled,
                schema4_authority_shadow_enabled=schema4_authority_shadow_enabled,
                schema4_authority_shadow_state=schema4_authority_shadow_state,
                authoritative_schema4_runtime_enabled=(
                    authoritative_schema4_runtime_enabled
                ),
                schema4_authority_consumer_shadow_enabled=(
                    schema4_authority_consumer_shadow_enabled
                ),
                schema4_authority_production_cutover_enabled=(
                    schema4_authority_production_cutover_enabled
                ),
                _authoritative_schema4_backend=schema4_backend,
                _schema4_startup_preparation=schema4_startup_preparation,
            )
        except Exception:
            if schema4_backend is not None:
                schema4_backend.close(flush=False)
            raise

    @classmethod
    def initialize_with_startup_fallback(
        cls,
        *,
        active_path: str | Path,
        legacy_path: str | Path,
        **kwargs: object,
    ) -> "Phase2RestartRuntime":
        """Keep the application usable when persistent startup fails safely."""

        try:
            return cls.initialize(
                active_path=active_path,
                legacy_path=legacy_path,
                **kwargs,
            )
        except Exception as exc:
            logger.exception(
                "Server Companion restart-learning initialization failed; "
                "continuing with persistence disabled"
            )
            return cls.disabled_for_startup_failure(
                active_path=active_path,
                error=f"{type(exc).__name__}: {exc}",
                error_kind=_startup_failure_kind(exc),
            )

    @classmethod
    def disabled_for_startup_failure(
        cls,
        *,
        active_path: str | Path,
        error: str,
        error_kind: str = "startup_initialization_failure",
    ) -> "Phase2RestartRuntime":
        """Create a non-persisting learner without reading or writing disk."""

        migration = Phase2MigrationResult(
            state=new_phase2_state(now=0),
            status=Phase2InitializationStatus.FAILED,
            reset_performed=False,
            recovery_performed=False,
            active_path=Path(active_path),
            backup_path=None,
            legacy_checksum=None,
            persistence_enabled=False,
            error_kind=str(error_kind),
            error=str(error),
        )
        return cls(migration, now=0)

    @property
    def persistence_enabled(self) -> bool:
        return self.persistence_status is RuntimePersistenceStatus.ENABLED

    @property
    def pending_notice(self) -> RuntimeNotice | None:
        result = self.migration
        if self.persistence_status is RuntimePersistenceStatus.DISABLED_WRITE_FAILED:
            detail = str(self.persistence_error or "unknown persistence failure")
            return RuntimeNotice(
                kind="persistence_write_failed",
                title="Server Companion Learning Is Not Being Saved",
                body=(
                    "Current Server Companion information remains available, but new "
                    "restart-learning changes cannot be saved or retained after this "
                    "launch. Existing learning files were not reset or deleted. "
                    f"Technical error: {detail}"
                ),
            )
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
        if server.engine.active_episode is None:
            server.engine = PhysicalEpisodeEngine(self.detection_config)
        else:
            # A confirmed durable episode may span an application restart.  Keep
            # its immutable event identity and semantic state, while explicitly
            # breaking high-authority continuity across the process boundary.
            server.engine.active_episode.coverage_complete = False
            server.engine.active_episode.reason_codes.add(
                "application_restart_episode_resumed"
            )
        server.app_session_id = self.app_session_id
        server.monitoring_session_id = str(uuid.uuid4())
        server.poll_generation = _nonnegative_int(poll_generation, 0)
        server.continuity_chain_id = deterministic_continuity_chain_id(
            server.key,
            server.app_session_id,
            server.monitoring_session_id,
            server.poll_generation,
            wall_at,
        )
        server.previous_sample = None
        server.player_observations.clear()
        server.monitoring_sessions.append({
            "session_id": server.monitoring_session_id,
            "app_session_id": server.app_session_id,
            "poll_generation": server.poll_generation,
            "continuity_chain_id": server.continuity_chain_id,
            "continuity_chain_ids": [server.continuity_chain_id],
            "provenance_version": CONTINUITY_PROVENANCE_VERSION,
            "started_at": wall_at,
            "ended_at": None,
            "reason": None,
        })
        server.monitoring_sessions[:] = server.monitoring_sessions[-MAX_MONITORING_SESSIONS:]
        server.dirty = True
        self._persist_server(server, force=True, now=wall_at)
        return server.monitoring_session_id

    def ensure_monitoring_session(
        self,
        server_key: str,
        *,
        wall_at: float,
        monotonic_at: float,
        poll_generation: int,
    ) -> tuple[str, bool]:
        """Return a matching active session, creating one only when required."""

        if not self.persistence_enabled:
            return "", False
        server = self._servers.get(str(server_key))
        generation = _nonnegative_int(poll_generation, 0)
        if (
            server is not None
            and server.monitoring_session_id
            and server.poll_generation == generation
        ):
            return server.monitoring_session_id, False
        return (
            self.begin_monitoring(
                server_key,
                wall_at=wall_at,
                monotonic_at=monotonic_at,
                poll_generation=generation,
            ),
            True,
        )

    def active_monitoring_session_id(self, server_key: str) -> str:
        server = self._servers.get(str(server_key))
        return "" if server is None else server.monitoring_session_id

    def end_monitoring_if_current(
        self,
        server_key: str,
        *,
        marker: LifecycleMarker,
        wall_at: float,
        monotonic_at: float,
        expected_session_id: str,
        expected_poll_generation: int,
    ) -> RuntimeUpdate:
        """End only the exact session/generation that requested the lifecycle."""

        server = self._servers.get(str(server_key))
        if server is None or not server.monitoring_session_id:
            return RuntimeUpdate(
                accepted=False,
                rejected_reason="no_active_monitoring_session",
            )
        if (
            server.monitoring_session_id != str(expected_session_id)
            or server.poll_generation
            != _nonnegative_int(expected_poll_generation, 0)
        ):
            return RuntimeUpdate(
                accepted=False,
                rejected_reason="stale_monitoring_lifecycle",
            )
        return self.end_monitoring(
            server_key,
            marker=marker,
            wall_at=wall_at,
            monotonic_at=monotonic_at,
        )

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
            continuity_chain_id=server.continuity_chain_id,
            provenance_version=CONTINUITY_PROVENANCE_VERSION,
        )
        update = self._ingest_sample(server, sample, persist_immediately=True)
        for session in reversed(server.monitoring_sessions):
            if session.get("session_id") == server.monitoring_session_id:
                session["ended_at"] = wall_at
                session["reason"] = marker.value
                break
        server.monitoring_session_id = ""
        server.continuity_chain_id = ""
        server.previous_sample = None
        server.player_observations.clear()
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
            continuity_chain_id=server.continuity_chain_id,
            provenance_version=CONTINUITY_PROVENANCE_VERSION,
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
        if sample.continuity_chain_id is None:
            sample = replace(
                sample,
                continuity_chain_id=server.continuity_chain_id,
                provenance_version=CONTINUITY_PROVENANCE_VERSION,
            )
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

    def recovery_alert_expected_window(
        self, server_key: str
    ) -> RecoveryAlertExpectedWindow:
        """Return transient alert-window policy for the active physical episode."""

        server = self._servers.get(str(server_key))
        episode = server.engine.active_episode if server is not None else None
        event_id = str(episode.event_id) if episode is not None else None
        restart_started_at = (
            float(episode.first_failure_wall)
            if episode is not None and episode.first_failure_wall is not None
            else None
        )
        resolution = self._authority_consumer_resolutions.get(str(server_key))
        if resolution is None:
            return classify_recovery_alert_expected_window(
                event_id=event_id,
                restart_started_at=restart_started_at,
                safe_prediction=False,
                candidate_period_seconds=None,
                predicted_occurrence_at=None,
            )

        from .companion_restart_phase2_authority_consumers import (
            AuthorityConsumerDecision,
            CutoverSource,
        )

        selected = resolution.selected_output
        safe = False
        period = None
        prediction = None
        if resolution.source is CutoverSource.SCHEMA4 and isinstance(
            selected, AuthorityConsumerDecision
        ):
            backend = self._authoritative_schema4_backend
            authority = (
                backend.authority_decision(str(server_key))
                if backend is not None
                else None
            )
            safe = bool(
                authority is not None
                and authority.state
                in {RegimeState.ESTABLISHED, RegimeState.NEW_REGIME_ESTABLISHED}
                and selected.countdown_visible
                and selected.countdown_safe
                and not selected.prediction_suspended
            )
            period = selected.cycle_period_seconds
            prediction = selected.next_expected_restart_at
        elif isinstance(selected, ConsumerDecision):
            safe = bool(
                selected.prediction_usable
                and selected.countdown_usable
                and selected.regime_status is RegimeStatus.STABLE
                and selected.model_status is ConsumerModelStatus.CONFIRMED_PERIOD
            )
            period = selected.selected_period_seconds
            prediction = selected.prediction_at

        return classify_recovery_alert_expected_window(
            event_id=event_id,
            restart_started_at=restart_started_at,
            safe_prediction=safe,
            candidate_period_seconds=period,
            predicted_occurrence_at=prediction,
        )

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
        backend = self._authoritative_schema4_backend
        if backend is not None:
            backend.close(flush=True)

    def authoritative_schema4_snapshot(self) -> object | None:
        """Read-only backend diagnostics; never consumed by Stage 3A policy."""

        backend = self._authoritative_schema4_backend
        if not self.authoritative_schema4_runtime_enabled or backend is None:
            return None
        return backend.snapshot()

    def export_authoritative_schema4_snapshot(self) -> object:
        """Expose the backend's locked, validated export without lifecycle changes."""

        backend = self._authoritative_schema4_backend
        if not self.authoritative_schema4_runtime_enabled or backend is None:
            raise RuntimeError("authoritative schema-4 learning export is unavailable")
        return backend.export_snapshot()

    def authority_consumer_shadow_snapshot(self, server_key: str) -> object | None:
        """Return an in-memory Stage 3B1 comparison; never a production input."""

        if not (
            self.schema4_authority_consumer_shadow_enabled
            or self.schema4_authority_production_cutover_enabled
        ):
            return None
        return self._authority_consumer_comparisons.get(str(server_key))

    def authority_consumer_resolution(self, server_key: str) -> object | None:
        """Expose the gated future resolver without changing ``decision()``."""

        return self._authority_consumer_resolutions.get(str(server_key))

    def authority_consumer_cutover_summary(
        self, server_key: str, *, now: float
    ) -> dict | None:
        """Return schema-4 presentation only when the explicit resolver selected it."""

        if not self.schema4_authority_production_cutover_enabled:
            return None
        from .companion_restart_phase2_authority_consumers import (
            AuthorityConsumerDecision,
            CutoverSource,
            authority_consumer_summary,
        )

        resolution = self._authority_consumer_resolutions.get(str(server_key))
        if resolution is None or resolution.source is not CutoverSource.SCHEMA4:
            return None
        if not isinstance(resolution.selected_output, AuthorityConsumerDecision):
            return None
        return authority_consumer_summary(resolution.selected_output, now=now)

    def dispatch_authority_consumer_action(
        self,
        server_key: str,
        *,
        kind: str,
        now: float,
        action: Callable[[], bool],
        after_reservation: Callable[[], None] | None = None,
        after_dispatch: Callable[[], None] | None = None,
    ) -> AuthorityConsumerActionResult:
        """Reserve durably, then invoke one existing external action at most once."""

        from .companion_restart_phase2_authority_consumers import (
            AuthorityConsumerDecision,
            CutoverSource,
        )

        if not self.schema4_authority_production_cutover_enabled:
            return AuthorityConsumerActionResult(
                False, False, "schema3", None, None, ("production_cutover_disabled",)
            )
        resolution = self._authority_consumer_resolutions.get(str(server_key))
        if resolution is None or resolution.source is not CutoverSource.SCHEMA4:
            return AuthorityConsumerActionResult(
                False,
                False,
                "schema3_safe_fallback",
                None,
                None,
                ("schema4_consumer_not_selected",),
            )
        decision = resolution.selected_output
        if not isinstance(decision, AuthorityConsumerDecision):
            return AuthorityConsumerActionResult(
                False, False, "schema3_safe_fallback", None, None, ("invalid_consumer_output",)
            )
        if kind == "scheduled_warning":
            eligible = decision.scheduled_pre_restart_alert_eligible
            key = decision.scheduled_warning_suppression_key
        elif kind == "generic_recovery":
            eligible = decision.generic_recovery_alert_eligible
            key = decision.generic_recovery_suppression_key
        else:
            raise ValueError("unsupported authority consumer action kind")
        if not eligible or key is None:
            return AuthorityConsumerActionResult(
                False,
                False,
                "schema4",
                None if key is None else key.serialize(),
                None,
                ("schema4_action_not_eligible",),
            )
        backend = self._authoritative_schema4_backend
        if backend is None or not self.authoritative_schema4_runtime_enabled:
            return AuthorityConsumerActionResult(
                False, False, "schema3_safe_fallback", None, None, ("schema4_backend_unavailable",)
            )
        reservation = backend.reserve_consumer_alert(
            decision=decision,
            key=key,
            created_at=now,
        )
        if not reservation.reserved:
            return AuthorityConsumerActionResult(
                False,
                False,
                "schema4",
                key.serialize(),
                None,
                reservation.reason_codes,
            )
        if after_reservation is not None:
            after_reservation()
        succeeded = False
        try:
            succeeded = bool(action())
            if after_dispatch is not None:
                after_dispatch()
        except Exception:
            succeeded = False
        completed = backend.complete_consumer_alert(
            decision=decision,
            reservation=reservation,
            completed_at=now,
            action_succeeded=succeeded,
        )
        return AuthorityConsumerActionResult(
            True,
            succeeded,
            "schema4",
            key.serialize(),
            completed.generation,
            (
                "external_action_emitted_once"
                if succeeded
                else "external_action_failed_after_reservation",
                "at_most_once_delivery",
            ),
        )

    def continuity_shadow_snapshot(
        self, server_key: str
    ) -> ContinuityShadowSnapshot | None:
        """Expose diagnostics only when the development shadow is enabled."""

        if not self.continuity_shadow_enabled:
            return None
        server = self._servers.get(str(server_key))
        tracker = self._continuity_shadow_trackers.get(str(server_key))
        if server is None or tracker is None:
            return ContinuityShadowSnapshot((), (), ())
        return tracker.snapshot(server.events)

    def expected_window_v2_shadow_snapshot(
        self, server_key: str
    ) -> tuple[ExpectedWindowLedgerRecord, ...] | None:
        """Expose non-authoritative v2 results only when explicitly enabled."""

        if not self.expected_window_v2_shadow_enabled:
            return None
        return tuple(self._expected_window_v2_shadow_ledgers.get(str(server_key), ()))

    def continuity_authority_shadow_snapshot(
        self, server_key: str
    ) -> AuthorityDecision | None:
        """Expose the immutable non-authoritative Stage 2B1 decision."""

        if not self.continuity_authority_shadow_enabled:
            return None
        return self._continuity_authority_shadow_decisions.get(str(server_key))

    def schema4_authority_shadow_snapshot(self, server_key: str) -> object | None:
        """Expose persisted/in-memory comparison only when explicitly enabled."""

        if not self.schema4_authority_shadow_enabled:
            return None
        return self._schema4_authority_shadow_comparisons.get(str(server_key))

    def _ingest_sample(
        self,
        server: _ServerRuntime,
        sample: ObservationSample,
        *,
        persist_immediately: bool = False,
    ) -> RuntimeUpdate:
        tracker = None
        if self.continuity_shadow_enabled:
            tracker = self._continuity_shadow_trackers.setdefault(
                server.key, ContinuityShadowTracker(self.detection_config)
            )
            tracker.observe(sample)
        previous_sample = server.previous_sample
        if (
            previous_sample is not None
            and sample.lifecycle is LifecycleMarker.NORMAL
        ):
            edge = span_from_observations(
                previous_sample,
                sample,
                config=self.detection_config,
            )
            if continuity_span_terminates_chain(edge):
                prior_chain_id = server.continuity_chain_id
                server.continuity_chain_id = deterministic_continuity_chain_id(
                    server.key,
                    server.app_session_id,
                    server.monitoring_session_id,
                    server.poll_generation,
                    sample.wall_at,
                )
                sample = replace(
                    sample,
                    continuity_chain_id=server.continuity_chain_id,
                    provenance_version=CONTINUITY_PROVENANCE_VERSION,
                )
                for session in reversed(server.monitoring_sessions):
                    if session.get("session_id") != server.monitoring_session_id:
                        continue
                    chain_ids = [
                        str(item)
                        for item in session.get("continuity_chain_ids", ())
                        if isinstance(item, str) and item
                    ]
                    if prior_chain_id and prior_chain_id not in chain_ids:
                        chain_ids.append(prior_chain_id)
                    if server.continuity_chain_id not in chain_ids:
                        chain_ids.append(server.continuity_chain_id)
                    session["continuity_chain_ids"] = chain_ids
                    break
                if tracker is not None:
                    tracker.begin_chain(sample)
                    logger.debug(
                        "restart continuity shadow chain boundary kind=%s old=%s new=%s",
                        edge.kind.value,
                        prior_chain_id,
                        server.continuity_chain_id,
                    )
                server.dirty = True
        previous_state = server.engine.state
        previous_active_episode = server.engine.active_episode
        previous_episode = _active_episode_persistence_signature(
            server.engine.active_episode,
            config=self.detection_config,
        )
        events = server.engine.ingest(sample)
        active_episode = server.engine.active_episode
        query_visible_repopulation_event_id = None
        if (
            previous_active_episode is None
            and active_episode is not None
            and sample.info_status is InfoStatus.HEALTHY
            and sample.player_status is FieldStatus.PRESENT
            and sample.players is not None
            and sample.players > 0
            and "standalone_query_visible_drain_promoted"
            in active_episode.reason_codes
            and active_episode.first_failure_mono is None
            and active_episode.confirmed_offline_mono is None
        ):
            query_visible_repopulation_event_id = active_episode.event_id
        self._record_player_observation(server, sample)
        self._extend_coverage(
            server,
            sample,
            previous_engine_state=previous_state,
            current_engine_state=server.engine.state,
        )
        state_changed = (
            server.engine.state is not previous_state
            or _active_episode_persistence_signature(
                server.engine.active_episode,
                config=self.detection_config,
            )
            != previous_episode
            or bool(events)
            or sample.lifecycle is not LifecycleMarker.NORMAL
        )
        server.previous_sample = sample if sample.lifecycle is LifecycleMarker.NORMAL else None
        if events:
            events = self._route_finalized(server, events, now=sample.wall_at)
        if self.continuity_shadow_enabled and (
            events or sample.lifecycle is not LifecycleMarker.NORMAL
        ):
            self._log_continuity_shadow(server, events)
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
            query_visible_repopulation_event_id=(
                query_visible_repopulation_event_id
            ),
            decision=decision,
            compatibility=compatibility_summary(decision),
            state_changed=state_changed or misses_changed,
            persisted=persisted,
        )

    def _log_continuity_shadow(
        self,
        server: _ServerRuntime,
        events: Iterable[PhysicalRestartEvent],
    ) -> None:
        tracker = self._continuity_shadow_trackers.get(server.key)
        if tracker is None:
            return
        snapshot = tracker.snapshot(server.events)
        for chain in snapshot.chains:
            identity = f"chain:{chain.chain_id}:{chain.termination_reason}"
            if identity in self._continuity_shadow_logged_ids:
                continue
            self._continuity_shadow_logged_ids.add(identity)
            logger.debug(
                "restart continuity shadow chain id=%s lifecycle=%s termination=%s reasons=%s",
                chain.chain_id,
                chain.lifecycle.value,
                chain.termination_reason,
                ",".join(chain.reason_codes),
            )
        event_ids = {item.event_id for item in events}
        for relationship in snapshot.relationships:
            if not event_ids.intersection(
                {relationship.left_event_id, relationship.right_event_id}
            ):
                continue
            identity = f"relationship:{relationship.relationship_id}"
            if identity in self._continuity_shadow_logged_ids:
                continue
            self._continuity_shadow_logged_ids.add(identity)
            logger.debug(
                "restart continuity shadow relationship id=%s high=%s reasons=%s",
                relationship.relationship_id,
                relationship.high_authority_eligible,
                ",".join(relationship.reason_codes),
            )
        for streak in snapshot.streaks:
            if not event_ids.intersection(streak.event_ids):
                continue
            identity = f"streak:{streak.streak_id}"
            if identity in self._continuity_shadow_logged_ids:
                continue
            self._continuity_shadow_logged_ids.add(identity)
            logger.debug(
                "restart continuity shadow streak id=%s intervals=%s reasons=%s",
                streak.streak_id,
                streak.interval_count,
                ",".join(streak.reason_codes),
            )

    def _extend_coverage(
        self,
        server: _ServerRuntime,
        sample: ObservationSample,
        *,
        previous_engine_state: EpisodeState,
        current_engine_state: EpisodeState,
    ) -> None:
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
        previous_confirmed_offline = previous_engine_state is EpisodeState.OFFLINE
        threshold = (
            max(12.0, 4.0 * self.detection_config.offline_poll_interval)
            if previous_confirmed_offline
            else max(30.0, 3.0 * self.detection_config.online_poll_interval)
        )
        if elapsed > threshold:
            kind = CoverageKind.SLEEP_GAP
        elif previous.info_status is InfoStatus.HEALTHY and sample.info_status is InfoStatus.HEALTHY:
            if previous.player_status is FieldStatus.PRESENT and sample.player_status is FieldStatus.PRESENT:
                kind = CoverageKind.ONLINE_HEALTHY
            else:
                kind = CoverageKind.MISSING_PLAYERS
        elif previous.info_status in {InfoStatus.NEUTRAL, InfoStatus.ERROR, InfoStatus.MISSING} or sample.info_status in {
            InfoStatus.NEUTRAL,
            InfoStatus.ERROR,
            InfoStatus.MISSING,
        }:
            kind = CoverageKind.QUERY_HEALTH_GAP
        elif (
            previous_engine_state is EpisodeState.OFFLINE
            and current_engine_state is EpisodeState.OFFLINE
            and previous.info_status in {InfoStatus.TIMEOUT, InfoStatus.NETWORK_ERROR}
            and sample.info_status in {InfoStatus.TIMEOUT, InfoStatus.NETWORK_ERROR}
        ):
            kind = CoverageKind.OFFLINE_OBSERVED
        else:
            # A healthy/failure boundary is timestamped by two real polls. Leave
            # the short edge unfilled so CoverageTimeline applies its cadence
            # gap limit; labelling the whole edge unhealthy would make every
            # genuine observed outage destroy otherwise continuous coverage.
            return
        cadence = (
            self.detection_config.offline_poll_interval
            if previous_confirmed_offline
            else self.detection_config.online_poll_interval
        )
        segment = CoverageSegment(
            previous.wall_at,
            sample.wall_at,
            kind,
            cadence,
            server_key=server.key,
            app_session_id=sample.app_session_id,
            monitoring_session_id=sample.monitoring_session_id,
            poll_generation=sample.poll_generation,
            continuity_chain_id=sample.continuity_chain_id,
            provenance_version=sample.provenance_version,
        )
        if (
            server.coverage
            and server.coverage[-1].kind is segment.kind
            and server.coverage[-1].end_at == segment.start_at
            and server.coverage[-1].cadence == segment.cadence
            and server.coverage[-1].continuity_chain_id
            == segment.continuity_chain_id
        ):
            prior = server.coverage[-1]
            server.coverage[-1] = replace(prior, end_at=segment.end_at)
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

    def _fold_old_events(
        self,
        server: _ServerRuntime,
        *,
        now: float,
        target_limit: int = MAX_FINALIZED_EVENTS,
    ) -> None:
        trim = len(server.events) - max(0, int(target_limit))
        if trim <= 0:
            return
        # Fold one boundary pair at a time. Scoring only the removed prefix loses
        # the adjacent interval from its last event to the first retained event;
        # feeding the existing aggregate back into that score can also compound
        # already-folded evidence. A two-event, aggregate-free score captures the
        # one semantic relationship that disappears at each trim exactly once.
        for _index in range(trim):
            removed = server.events[0]
            boundary = server.events[1] if len(server.events) > 1 else None
            pair_events = (removed, boundary) if boundary is not None else (removed,)
            pair_score = self.scorer.score(
                pair_events,
                CoverageTimeline(tuple(server.coverage)),
                now=now,
                phase_recency_policy=PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
            )
            candidates = []
            removed_at = _event_at(removed)
            for scored in pair_score.candidates:
                prior = server.aggregate.candidate(scored.period_seconds)
                has_hint = scored.hints.event_count > 0 and removed_at is not None
                # Each endpoint eventually becomes the removed endpoint. Retain
                # only this endpoint's share now so the boundary is not counted
                # again on the next trim.
                hint_share = (
                    scored.hints.weighted_alignment / scored.hints.event_count
                    if has_hint
                    else 0.0
                )
                candidates.append(
                    CandidateAggregate(
                        period_seconds=scored.period_seconds,
                        direct_count=prior.direct_count + scored.strict_direct_interval_count,
                        direct_weight=prior.direct_weight + scored.direct_support,
                        miss_count=prior.miss_count + scored.covered_miss_count,
                        miss_penalty=prior.miss_penalty + scored.covered_miss_penalty,
                        off_grid_count=prior.off_grid_count + scored.off_grid_event_count,
                        off_grid_penalty=prior.off_grid_penalty + scored.off_grid_penalty,
                        hint_count=prior.hint_count + int(has_hint),
                        hint_weight=prior.hint_weight + hint_share,
                        first_hint_at=_minimum_optional(
                            prior.first_hint_at, removed_at if has_hint else None
                        ),
                        last_hint_at=_maximum_optional(
                            prior.last_hint_at, removed_at if has_hint else None
                        ),
                        phase_vector_sin=prior.phase_vector_sin,
                        phase_vector_cos=prior.phase_vector_cos,
                        divisor_resolution_count=(
                            prior.divisor_resolution_count
                            + sum(
                                item.covered_window_count
                                for item in scored.divisor_resolution
                            )
                        ),
                    )
                )
            source_counts = dict(server.aggregate.source_quality_counts)
            source_counts[removed.outcome.value] = (
                source_counts.get(removed.outcome.value, 0) + 1
            )
            server.aggregate = LongTermAggregate(
                semantics_version=AGGREGATE_SEMANTICS_VERSION,
                candidates=tuple(candidates),
                source_quality_counts=tuple(sorted(source_counts.items())),
                first_observation_at=_minimum_optional(
                    server.aggregate.first_observation_at, removed_at
                ),
                last_observation_at=_maximum_optional(
                    server.aggregate.last_observation_at, removed_at
                ),
                anomaly_count=server.aggregate.anomaly_count
                + int(
                    removed.outcome
                    in {
                        EventOutcome.AMBIGUOUS_DRAIN,
                        EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
                    }
                ),
                prior_regimes=server.aggregate.prior_regimes,
            )
            server.folded_through_event_seq = max(
                server.folded_through_event_seq, removed.sequence
            )
            server.events.pop(0)

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
            phase_recency_policy=PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
            previous=previous,
            incumbent_period_seconds=server.incumbent_period_seconds,
            aggregate=server.aggregate,
            expected_misses=server.expected_misses,
            regime_boundaries=(item.ended_at for item in server.prior_regimes),
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
        if self.continuity_authority_shadow_enabled:
            self._evaluate_continuity_authority_shadow(server)
        if (
            self.schema4_authority_consumer_shadow_enabled
            or self.schema4_authority_production_cutover_enabled
        ):
            self._evaluate_authority_consumer_shadow(
                server,
                schema3_decision=decision,
                now=now,
                server_online_healthy=server_online_healthy,
                restart_alert_enabled=restart_alert_enabled,
                event=event,
            )
        return decision

    def _evaluate_authority_consumer_shadow(
        self,
        server: _ServerRuntime,
        *,
        schema3_decision: ConsumerDecision,
        now: float,
        server_online_healthy: bool,
        restart_alert_enabled: bool,
        event: PhysicalRestartEvent | None,
    ) -> None:
        from .companion_restart_phase2_authority_consumers import (
            AuthorityConsumerPolicyInput,
            NormalPredictionEvidence,
            compare_authority_consumers,
            evaluate_authority_consumers,
            resolve_authority_consumer_cutover,
        )

        backend = self._authoritative_schema4_backend
        authority_decision = None
        valid = False
        if self.authoritative_schema4_runtime_enabled and backend is not None:
            valid = bool(backend.server_authority_valid(server.key))
            if valid:
                authority_decision = backend.authority_decision(server.key)
        if authority_decision is None:
            authority_decision = self._continuity_authority_shadow_decisions.get(
                server.key
            )
            valid = authority_decision is not None

        consumer = None
        if authority_decision is not None:
            fired_keys = (
                backend.consumer_fired_keys(server.key)
                if self.authoritative_schema4_runtime_enabled
                and backend is not None
                and valid
                else frozenset()
            )
            selected = None
            if server.score is not None and server.score.selected_period_seconds is not None:
                selected = server.score.candidate(server.score.selected_period_seconds)
            unresolved_required_divisor = bool(
                selected is not None
                and GATES[selected.period_seconds].require_divisors
                and (
                    not selected.divisor_resolution
                    or not all(
                        item.established_resolved
                        for item in selected.divisor_resolution
                    )
                )
            )
            normal_prediction_evidence = (
                NormalPredictionEvidence(
                    selected_period_seconds=server.score.selected_period_seconds,
                    incumbent_period_seconds=server.score.incumbent_period_seconds,
                    schedule_existence_confidence=(
                        server.score.schedule_existence_confidence
                    ),
                    fundamental_period_confidence=(
                        selected.fundamental_period_confidence
                    ),
                    phase_confidence=selected.phase_confidence,
                    candidate_established=selected.establishment_gates_passed,
                    strict_direct_relationship_count=(
                        selected.strict_direct_interval_count
                    ),
                    unresolved_competitor=bool(
                        server.score.unresolved_competitors
                    ),
                    unresolved_divisor_or_harmonic=(
                        unresolved_required_divisor
                    ),
                    regime_stable=(
                        server.score.regime_status is RegimeStatus.STABLE
                    ),
                    latest_aligned_phase_at=(
                        selected.recent_phase_observation_at
                    ),
                    raw_predicted_occurrence_at=_next_prediction(
                        server.score, now
                    ),
                    recent_strong_anomaly=bool(
                        selected.strong_covered_contradiction
                        or BlockReason.RECENT_STRONG_ANOMALY
                        in schema3_decision.prediction_reasons
                    ),
                )
                if server.score is not None and selected is not None
                else None
            )
            consumer = evaluate_authority_consumers(
                AuthorityConsumerPolicyInput(
                    authority_decision=authority_decision,
                    now=now,
                    server_online_healthy=server_online_healthy,
                    restart_alert_enabled=restart_alert_enabled,
                    physical_recovery_event_id=(event.event_id if event else None),
                    physical_recovery_eligible=schema3_decision.generic_recovery_eligible,
                    fired_keys=fired_keys,
                    normal_pattern_supported=(
                        schema3_decision.model_status is not ConsumerModelStatus.NO_PATTERN
                    ),
                    normal_pattern_confidence=(
                        schema3_decision.schedule_existence_confidence
                    ),
                    normal_prediction_evidence=normal_prediction_evidence,
                )
            )
            self._authority_consumer_shadow_decisions[server.key] = consumer
            if (
                self.schema4_authority_production_cutover_enabled
                and self.authoritative_schema4_runtime_enabled
                and backend is not None
                and valid
                and server_online_healthy
            ):
                backend.record_consumer_decision(consumer, created_at=now)
            comparison = compare_authority_consumers(schema3_decision, consumer)
            self._authority_consumer_comparisons[server.key] = comparison
            if (
                comparison.significant_difference
                and comparison.comparison_id not in self._authority_consumer_logged_ids
            ):
                self._authority_consumer_logged_ids.add(comparison.comparison_id)
                logger.debug(
                    "restart authority consumer shadow server=%s comparison=%s "
                    "schema3=%s schema4=%s differences=%s",
                    server.key,
                    comparison.comparison_id,
                    comparison.schema3_presentation_key,
                    comparison.schema4_presentation_key,
                    ",".join(comparison.reason_code_differences),
                )
        self._authority_consumer_resolutions[server.key] = (
            resolve_authority_consumer_cutover(
                schema3_decision=schema3_decision,
                schema4_decision=consumer,
                production_cutover_enabled=(
                    self.schema4_authority_production_cutover_enabled
                ),
                authoritative_schema4_runtime_enabled=(
                    self.authoritative_schema4_runtime_enabled
                ),
                schema4_server_valid=valid,
            )
        )

    def _evaluate_continuity_authority_shadow(
        self, server: _ServerRuntime
    ) -> None:
        """Reconstruct and evaluate authority without changing production state."""

        if server.score is None:
            return
        reconstructed_events = tuple(
            reconstruct_historical_event_provenance(
                item, server.monitoring_sessions
            ).event
            for item in server.events
        )
        spans = continuity_spans_from_coverage(
            server.coverage,
            server_key=server.key,
            monitoring_sessions=server.monitoring_sessions,
        )
        transition_spans = observed_transition_spans_from_events(
            reconstructed_events
        )
        span_values = tuple(
            {
                item.reference_id: item for item in (*spans, *transition_spans)
            }.values()
        )
        chains = build_continuity_chains(span_values)
        relationships = extract_interval_relationships(
            reconstructed_events, span_values
        )
        streaks = build_cadence_streaks(relationships, chains=chains)
        previous = self._continuity_authority_shadow_decisions.get(server.key)
        decision = evaluate_authority(
            server_key=server.key,
            normal_score=server.score,
            relationships=relationships,
            streaks=streaks,
            expected_window_ledger=self._expected_window_v2_shadow_ledgers.get(
                server.key, ()
            ),
            events=reconstructed_events,
            prior_decision=previous,
        )
        self._continuity_authority_shadow_decisions[server.key] = decision
        if self.schema4_authority_shadow_enabled:
            self._compare_schema4_authority_shadow(
                server,
                decision=decision,
                relationship_count=len(relationships),
                streak_count=len(streaks),
            )
        if decision.decision_id in self._continuity_authority_shadow_logged_ids:
            return
        self._continuity_authority_shadow_logged_ids.add(decision.decision_id)
        selected = decision.selected_shadow_regime
        challenger = decision.strongest_challenger
        logger.debug(
            "restart authority shadow decision=%s state=%s selected=%s period=%s "
            "challenger=%s high_relationships=%s streaks=%s reasons=%s",
            decision.decision_id,
            decision.state.value,
            selected.regime_id if selected is not None else "none",
            selected.candidate_period_seconds if selected is not None else "none",
            challenger.context_id if challenger is not None else "none",
            ",".join(decision.high_ledger.high_relationship_ids),
            ",".join(decision.high_ledger.maximal_streak_ids),
            ",".join(decision.reason_codes),
        )

    def _compare_schema4_authority_shadow(
        self,
        server: _ServerRuntime,
        *,
        decision: AuthorityDecision,
        relationship_count: int,
        streak_count: int,
    ) -> None:
        from .companion_restart_phase2_schema4 import compare_persisted_authority

        root = self._schema4_authority_shadow_state
        servers = root.get("servers") if isinstance(root, Mapping) else None
        record = servers.get(server.key) if isinstance(servers, Mapping) else None
        comparison = compare_persisted_authority(
            record=record if isinstance(record, Mapping) else None,
            in_memory_decision=decision,
            in_memory_relationship_count=relationship_count,
            in_memory_streak_count=streak_count,
            in_memory_expected_window_count=len(
                self._expected_window_v2_shadow_ledgers.get(server.key, ())
            ),
        )
        self._schema4_authority_shadow_comparisons[server.key] = comparison
        identity = (
            f"{comparison.in_memory_decision_id}:"
            f"{comparison.persisted_decision_id}:"
            f"{comparison.object_counts_equal}"
        )
        if identity in self._schema4_authority_shadow_logged_ids:
            return
        self._schema4_authority_shadow_logged_ids.add(identity)
        logger.debug(
            "restart schema4 authority shadow server=%s decision_equal=%s "
            "counts_equal=%s warnings=%s reasons=%s",
            server.key,
            comparison.decision_id_equal,
            comparison.object_counts_equal,
            ",".join(comparison.migration_warnings),
            ",".join(comparison.reason_codes),
        )

    @staticmethod
    def _record_player_observation(
        server: _ServerRuntime, sample: ObservationSample
    ) -> None:
        if (
            sample.lifecycle is not LifecycleMarker.NORMAL
            or sample.info_status is not InfoStatus.HEALTHY
            or sample.player_status is not FieldStatus.PRESENT
            or sample.players is None
        ):
            return
        cutoff = sample.wall_at - PLAYER_OBSERVATION_RETENTION_SECONDS
        server.player_observations[:] = [
            item for item in server.player_observations if item[0] >= cutoff
        ]
        server.player_observations.append((sample.wall_at, sample.players))

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
                candidate.fundamental_relationship_count < 2
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
                    population_observable = (
                        _query_visible_population_transition_observable(
                            server,
                            candidate,
                            window_start=window_start,
                            window_end=window_end,
                        )
                    )
                    if (
                        assessment.fully_covered
                        and assessment.query_health_adequate
                        and not assessment.unresolved_episode
                        and not blocked
                        and not observed
                        and population_observable is not False
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
        if (
            self.expected_window_v2_shadow_enabled
            or self.continuity_authority_shadow_enabled
        ):
            self._evaluate_expected_windows_v2_shadow(server, now=now)

    def _evaluate_expected_windows_v2_shadow(
        self, server: _ServerRuntime, *, now: float
    ) -> None:
        """Evaluate the same completed windows without affecting v1 authority."""

        score = server.score
        if score is None or not server.coverage:
            return
        ledger = self._expected_window_v2_shadow_ledgers.setdefault(server.key, [])
        spans = continuity_spans_from_coverage(
            server.coverage,
            server_key=server.key,
            monitoring_sessions=server.monitoring_sessions,
        )
        episodes = _expected_window_v2_episode_evidence(server)
        coverage_start = min(item.start_at for item in server.coverage)
        for candidate in score.candidates:
            if (
                candidate.fundamental_relationship_count < 2
                or candidate.phase_offset is None
                or candidate.phase_confidence <= 0
            ):
                continue
            period = candidate.period_seconds
            tolerance = candidate_phase_tolerance(period)
            start = max(coverage_start, now - 7 * 24 * 3600)
            multiplier = math.ceil((start - candidate.phase_offset) / period)
            expected = candidate.phase_offset + multiplier * period
            model_revision_id = deterministic_model_revision_id(
                server.key,
                period,
                candidate.phase_offset,
                (
                    f"normal-scorer-v{SCORING_SEMANTICS_VERSION}:"
                    f"relationships={candidate.fundamental_relationship_count}:"
                    f"phase={candidate.phase_confidence:.6f}"
                ),
            )
            checked = 0
            while expected + tolerance <= now and checked < 64:
                checked += 1
                result = classify_expected_window(
                    server_key=server.key,
                    candidate_period_seconds=period,
                    expected_phase_offset=candidate.phase_offset,
                    window_start_at=max(0.0, expected - tolerance),
                    expected_at=expected,
                    window_end_at=expected + tolerance,
                    model_revision_id=model_revision_id,
                    regime_interpretation_id=None,
                    events=server.events,
                    spans=spans,
                    episodes=episodes,
                    population_transition_observable=(
                        _query_visible_population_transition_observable(
                            server,
                            candidate,
                            window_start=max(0.0, expected - tolerance),
                            window_end=expected + tolerance,
                        )
                    ),
                )
                window_key = (server.key, period, int(round(expected)))
                existing_for_window = [
                    item
                    for item in ledger
                    if (
                        item.server_key,
                        item.candidate_period_seconds,
                        int(round(item.expected_at)),
                    )
                    == window_key
                ]
                new_records: tuple[ExpectedWindowLedgerRecord, ...] = ()
                if not existing_for_window:
                    ledger.append(result)
                    new_records = (result,)
                else:
                    reconciled = reconcile_expected_window_ledger(
                        ledger,
                        (result,),
                        reconciled_at=now,
                        evidence_revision_id=_expected_window_v2_evidence_revision(
                            server, result
                        ),
                    )
                    if len(reconciled) > len(ledger):
                        new_records = tuple(reconciled[len(ledger):])
                        ledger[:] = reconciled
                for record in new_records:
                    self._log_expected_window_v2_shadow(server, record)
                expected += period

    def _log_expected_window_v2_shadow(
        self,
        server: _ServerRuntime,
        record: ExpectedWindowLedgerRecord,
    ) -> None:
        if record.result_id in self._expected_window_v2_shadow_logged_ids:
            return
        self._expected_window_v2_shadow_logged_ids.add(record.result_id)
        v1_miss = any(
            item.period_seconds == record.candidate_period_seconds
            and int(round(item.expected_at)) == int(round(record.expected_at))
            for item in server.expected_misses
        )
        if isinstance(record, ExpectedWindowRevision):
            comparison = "proposed_retraction"
        elif v1_miss and record.outcome is ExpectedWindowOutcome.AMBIGUOUS:
            comparison = "v1_miss_v2_ambiguous"
        elif v1_miss and record.outcome is ExpectedWindowOutcome.UNKNOWN:
            comparison = "v1_miss_v2_unknown"
        elif v1_miss and record.outcome is ExpectedWindowOutcome.GENUINE_MISS:
            comparison = "v1_v2_genuine_miss_agreement"
        elif record.outcome is ExpectedWindowOutcome.HIT:
            comparison = "v2_hit"
        else:
            comparison = "v2_completed_window"
        logger.debug(
            "restart expected-window v2 shadow comparison=%s id=%s period=%s "
            "expected=%s outcome=%s evidence=%s reasons=%s",
            comparison,
            record.result_id,
            record.candidate_period_seconds,
            record.expected_at,
            record.outcome.value,
            ",".join(record.overlapping_outage_episode_ids),
            ",".join(record.reason_codes),
        )

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
        backend = self._authoritative_schema4_backend
        # Schema 4 persists completed durable evidence, not a growing healthy
        # poll edge.  A later event, lifecycle close, expected-window result,
        # regime change, compaction, or shutdown captures the coalesced span.
        if backend is not None and not force:
            return False
        if not force and now - server.last_saved_at < PERSIST_HEARTBEAT_SECONDS:
            return False
        if backend is not None:
            try:
                changed = backend.update_server_from_schema3(
                    server.key,
                    _serialize_server(server),
                    updated_at=now,
                    durable_reason=(
                        "schema4_chain_or_event_durable_update"
                        if force
                        else "schema4_coalesced_runtime_update"
                    ),
                )
                result = backend.flush() if force else None
            except Exception as exc:
                self.persistence_status = RuntimePersistenceStatus.DISABLED_WRITE_FAILED
                self.persistence_error = f"{type(exc).__name__}: {exc}"
                return False
            server.dirty = False
            server.last_saved_at = now
            return bool(changed and result is not None and result.wrote)
        servers = self.state.setdefault("servers", {})
        servers[server.key] = _serialize_server(server)
        self._compact_global_history(active_key=server.key, now=now)
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

    def _compact_global_history(self, *, active_key: str, now: float) -> None:
        """Bound total state while retaining compact knowledge for old identities."""

        ranked = sorted(
            self._servers.values(),
            key=lambda item: (_server_activity_at(item), item.key),
            reverse=True,
        )
        full_keys = {active_key}
        for item in ranked:
            if len(full_keys) >= MAX_FULL_DETAIL_SERVERS:
                break
            full_keys.add(item.key)
        remaining = [item for item in ranked if item.key not in full_keys]
        compact_keys = {
            item.key
            for item in remaining[: max(0, MAX_COMPACT_DETAIL_SERVERS - len(full_keys))]
        }
        serialized = self.state.setdefault("servers", {})
        for item in ranked:
            if item.key in full_keys:
                continue
            compact = item.key in compact_keys
            event_limit = COMPACT_EVENT_LIMIT if compact else COLD_EVENT_LIMIT
            coverage_limit = 24 if compact else 4
            session_limit = 24 if compact else 4
            miss_limit = 12 if compact else 0
            incomplete_limit = 10 if compact else 3
            fired_limit = 50 if compact else 10
            changed = False
            if len(item.events) > event_limit:
                self._fold_old_events(item, now=now, target_limit=event_limit)
                changed = True
            without_samples = [
                event if not event.samples else replace(event, samples=())
                for event in item.events
            ]
            if without_samples != item.events:
                item.events = without_samples
                changed = True
            if len(item.coverage) > coverage_limit:
                item.coverage = item.coverage[-coverage_limit:]
                changed = True
            if len(item.monitoring_sessions) > session_limit:
                item.monitoring_sessions = item.monitoring_sessions[-session_limit:]
                changed = True
            if len(item.expected_misses) > miss_limit:
                fold_count = len(item.expected_misses) - miss_limit
                self._fold_expected_misses(item, item.expected_misses[:fold_count])
                item.expected_misses = item.expected_misses[fold_count:]
                changed = True
            if len(item.incomplete_episodes) > incomplete_limit:
                item.incomplete_episodes = item.incomplete_episodes[-incomplete_limit:]
                changed = True
            if len(item.fired_keys) > fired_limit:
                item.fired_keys = set(
                    sorted(item.fired_keys, key=lambda value: value.serialize())[-fired_limit:]
                )
                changed = True
            retained_fingerprints = {event.fingerprint for event in item.events}
            if item.routed_fingerprints != retained_fingerprints:
                item.routed_fingerprints = retained_fingerprints
                changed = True
            if changed:
                item.dirty = True
                serialized[item.key] = _serialize_server(item)

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
                future_cutoff = now + PERSISTED_FUTURE_SKEW_SECONDS
                retained_events = [
                    item
                    for item in server.events
                    if _persisted_event_not_in_future(item, future_cutoff)
                ]
                retained_coverage = [
                    item for item in server.coverage if item.end_at <= future_cutoff
                ]
                retained_misses = [
                    item
                    for item in server.expected_misses
                    if item.expected_at <= future_cutoff
                ]
                if (
                    len(retained_events) != len(server.events)
                    or len(retained_coverage) != len(server.coverage)
                    or len(retained_misses) != len(server.expected_misses)
                ):
                    server.events = retained_events
                    server.coverage = retained_coverage
                    server.expected_misses = retained_misses
                    server.routed_fingerprints = {
                        item.fingerprint for item in retained_events
                    }
                    server.dirty = True
                server.score = self.scorer.score(
                    server.events,
                    CoverageTimeline(tuple(server.coverage)),
                    now=now,
                    phase_recency_policy=PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
                    incumbent_period_seconds=server.incumbent_period_seconds,
                    aggregate=server.aggregate,
                    expected_misses=server.expected_misses,
                    regime_boundaries=(
                        item.ended_at for item in server.prior_regimes
                    ),
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
    continuity_chain_id: str | None = None,
    provenance_version: int = 0,
) -> ObservationSample:
    payload = info if isinstance(info, dict) else {}
    disposition = live_result_disposition(payload)
    healthy = disposition is LiveResultDisposition.HEALTHY
    if disposition is LiveResultDisposition.HEALTHY:
        info_status = InfoStatus.HEALTHY
    elif disposition is LiveResultDisposition.NEUTRAL:
        info_status = InfoStatus.NEUTRAL
    elif disposition is LiveResultDisposition.QUALIFYING_FAILURE:
        classification = _live_result_classification(payload)
        info_status = (
            InfoStatus.TIMEOUT
            if classification in {"timeout", "explicit-offline"}
            else InfoStatus.NETWORK_ERROR
        )
    else:
        info_status = InfoStatus.ERROR

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
        continuity_chain_id=continuity_chain_id,
        provenance_version=provenance_version,
    )


def live_result_disposition(info: object) -> LiveResultDisposition:
    payload = info if isinstance(info, dict) else {}
    if payload.get("ok") is True:
        return LiveResultDisposition.HEALTHY
    if payload.get("neutral") is True or payload.get("outcome") == "alive-but-info-unavailable":
        return LiveResultDisposition.NEUTRAL
    classification = _live_result_classification(payload)
    if classification == "alive-but-info-unavailable":
        return LiveResultDisposition.NEUTRAL
    if classification in {"timeout", "socket-error", "explicit-offline"}:
        return LiveResultDisposition.QUALIFYING_FAILURE
    return LiveResultDisposition.PROTOCOL_FAILURE


def _live_result_classification(payload: dict) -> str:
    structured = str(payload.get("a2s_classification") or "").strip().lower()
    if not structured:
        diagnostics = payload.get("_a2s_diag")
        if isinstance(diagnostics, dict):
            structured = str(diagnostics.get("classification") or "").strip().lower()
    if structured:
        return structured
    error = payload.get("err") or payload.get("error")
    return result_classification(error)


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
            _serialize_active_episode(active, state=server.engine.state)
            if active is not None
            else None
        ),
        "incomplete_episodes": list(server.incomplete_episodes[-MAX_INCOMPLETE_EPISODES:]),
        "candidate_diagnostics": _serialize_score(server.score),
        "consumer_adapter_version": CONSUMER_ADAPTER_VERSION,
    }


def _serialize_active_episode(
    episode: object,
    *,
    state: EpisodeState,
) -> dict:
    """Serialize the last meaningful episode boundary for crash-safe recovery."""

    return {
        "snapshot_version": ACTIVE_EPISODE_SNAPSHOT_VERSION,
        "state": state.value,
        "sequence": episode.sequence,
        "server_key": episode.server_key,
        "app_session_id": episode.app_session_id,
        "monitoring_session_id": episode.monitoring_session_id,
        "poll_generation": episode.poll_generation,
        "started_wall": episode.started_wall,
        "started_mono": episode.started_mono,
        "fingerprint": episode.fingerprint,
        "event_id": episode.event_id,
        "continuity_chain_id": episode.continuity_chain_id,
        "provenance_version": episode.provenance_version,
        "baseline_players": episode.baseline_players,
        "baseline_wall": episode.baseline_wall,
        "baseline_mono": episode.baseline_mono,
        "last_positive_wall": episode.last_positive_wall,
        "last_positive_mono": episode.last_positive_mono,
        "drain_wall": episode.drain_wall,
        "drain_mono": episode.drain_mono,
        "low_start_wall": episode.low_start_wall,
        "low_start_mono": episode.low_start_mono,
        "zero_reached": episode.zero_reached,
        "minimum_players": episode.minimum_players,
        "drop_fraction": episode.drop_fraction,
        "abrupt_drain": episode.abrupt_drain,
        "drain_inherited": episode.drain_inherited,
        "low_samples": list(episode.low_samples),
        "low_sample_count": episode.low_sample_count,
        "first_failure_wall": episode.first_failure_wall,
        "first_failure_mono": episode.first_failure_mono,
        "last_failure_mono": episode.last_failure_mono,
        "failure_count": episode.failure_count,
        "failure_statuses": [item.value for item in episode.failure_statuses],
        "confirmed_offline_wall": episode.confirmed_offline_wall,
        "confirmed_offline_mono": episode.confirmed_offline_mono,
        "info_return_wall": episode.info_return_wall,
        "info_return_mono": episode.info_return_mono,
        "first_queue_wall": episode.first_queue_wall,
        "first_queue_mono": episode.first_queue_mono,
        "first_player_wall": episode.first_player_wall,
        "first_player_mono": episode.first_player_mono,
        "recovery_positive_times": list(episode.recovery_positive_times),
        "recovery_positive_count": episode.recovery_positive_count,
        "stable_recovery_wall": episode.stable_recovery_wall,
        "stable_recovery_mono": episode.stable_recovery_mono,
        "recovery_bounced": episode.recovery_bounced,
        "healthy_samples": episode.healthy_samples,
        "failed_samples": episode.failed_samples,
        "missing_player_samples": episode.missing_player_samples,
        "coverage_complete": episode.coverage_complete,
        "lifecycle_interruption": (
            episode.lifecycle_interruption.value
            if episode.lifecycle_interruption is not None
            else None
        ),
        "sources": sorted(item.value for item in episode.sources),
        "samples": [_serialize_sample(item) for item in episode.samples],
        "reason_codes": sorted(episode.reason_codes),
        "recovery_snapshot_unchanged": episode.recovery_snapshot_unchanged,
    }


def _deserialize_active_episode(
    server_key: str,
    raw: dict,
    config: DetectionConfig,
) -> PhysicalEpisodeEngine | None:
    """Restore versioned semantic episode state; legacy summaries stay incomplete."""

    if raw.get("snapshot_version") != ACTIVE_EPISODE_SNAPSHOT_VERSION:
        return None
    try:
        if _required_string(raw.get("server_key")) != server_key:
            raise ValueError("active episode server key mismatch")
        episode = detection_module._Episode(
            sequence=_nonnegative_int(raw.get("sequence"), 0),
            server_key=server_key,
            app_session_id=_required_string(raw.get("app_session_id")),
            monitoring_session_id=_required_string(
                raw.get("monitoring_session_id")
            ),
            poll_generation=_nonnegative_int(raw.get("poll_generation"), 0),
            started_wall=_safe_nonnegative_float(raw.get("started_wall"), 0.0),
            started_mono=_safe_nonnegative_float(raw.get("started_mono"), 0.0),
            fingerprint=_required_string(raw.get("fingerprint")),
            event_id=_required_string(raw.get("event_id")),
            continuity_chain_id=_optional_string(
                raw.get("continuity_chain_id")
            ),
            provenance_version=_nonnegative_int(
                raw.get("provenance_version"), 0
            ),
            baseline_players=_optional_count(raw.get("baseline_players")),
            baseline_wall=_optional_nonnegative_float(raw.get("baseline_wall")),
            baseline_mono=_optional_nonnegative_float(raw.get("baseline_mono")),
            last_positive_wall=_optional_nonnegative_float(
                raw.get("last_positive_wall")
            ),
            last_positive_mono=_optional_nonnegative_float(
                raw.get("last_positive_mono")
            ),
            drain_wall=_optional_nonnegative_float(raw.get("drain_wall")),
            drain_mono=_optional_nonnegative_float(raw.get("drain_mono")),
            low_start_wall=_optional_nonnegative_float(
                raw.get("low_start_wall")
            ),
            low_start_mono=_optional_nonnegative_float(
                raw.get("low_start_mono")
            ),
            zero_reached=bool(raw.get("zero_reached", False)),
            minimum_players=_optional_count(raw.get("minimum_players")),
            drop_fraction=_optional_unit_float(raw.get("drop_fraction")),
            abrupt_drain=bool(raw.get("abrupt_drain", False)),
            drain_inherited=bool(raw.get("drain_inherited", False)),
            low_samples=_finite_nonnegative_float_list(raw.get("low_samples"), 16),
            low_sample_count=_nonnegative_int(raw.get("low_sample_count"), 0),
            first_failure_wall=_optional_nonnegative_float(
                raw.get("first_failure_wall")
            ),
            first_failure_mono=_optional_nonnegative_float(
                raw.get("first_failure_mono")
            ),
            last_failure_mono=_optional_nonnegative_float(
                raw.get("last_failure_mono")
            ),
            failure_count=_nonnegative_int(raw.get("failure_count"), 0),
            failure_statuses=[
                InfoStatus(item)
                for item in raw.get("failure_statuses", ())
                if isinstance(item, str)
            ][-16:],
            confirmed_offline_wall=_optional_nonnegative_float(
                raw.get("confirmed_offline_wall")
            ),
            confirmed_offline_mono=_optional_nonnegative_float(
                raw.get("confirmed_offline_mono")
            ),
            info_return_wall=_optional_nonnegative_float(
                raw.get("info_return_wall")
            ),
            info_return_mono=_optional_nonnegative_float(
                raw.get("info_return_mono")
            ),
            first_queue_wall=_optional_nonnegative_float(
                raw.get("first_queue_wall")
            ),
            first_queue_mono=_optional_nonnegative_float(
                raw.get("first_queue_mono")
            ),
            first_player_wall=_optional_nonnegative_float(
                raw.get("first_player_wall")
            ),
            first_player_mono=_optional_nonnegative_float(
                raw.get("first_player_mono")
            ),
            recovery_positive_times=_finite_nonnegative_float_list(
                raw.get("recovery_positive_times"), 8
            ),
            recovery_positive_count=_nonnegative_int(
                raw.get("recovery_positive_count"), 0
            ),
            stable_recovery_wall=_optional_nonnegative_float(
                raw.get("stable_recovery_wall")
            ),
            stable_recovery_mono=_optional_nonnegative_float(
                raw.get("stable_recovery_mono")
            ),
            recovery_bounced=bool(raw.get("recovery_bounced", False)),
            healthy_samples=_nonnegative_int(raw.get("healthy_samples"), 0),
            failed_samples=_nonnegative_int(raw.get("failed_samples"), 0),
            missing_player_samples=_nonnegative_int(
                raw.get("missing_player_samples"), 0
            ),
            coverage_complete=bool(raw.get("coverage_complete", False)),
            lifecycle_interruption=(
                LifecycleMarker(raw.get("lifecycle_interruption"))
                if raw.get("lifecycle_interruption") is not None
                else None
            ),
            sources={
                SignalSource(item)
                for item in raw.get("sources", ())
                if isinstance(item, str)
            },
            samples=[
                sample
                for item in raw.get("samples", ())
                for sample in (_deserialize_sample(item),)
                if sample.server_key == server_key
            ],
            reason_codes={
                str(item)
                for item in raw.get("reason_codes", ())
                if isinstance(item, str) and item
            },
            recovery_snapshot_unchanged=bool(
                raw.get("recovery_snapshot_unchanged", False)
            ),
        )
        state = EpisodeState(raw.get("state"))
        if (
            state is EpisodeState.IDLE
            or episode.sequence < 1
            or (
                state is EpisodeState.OFFLINE
                and episode.confirmed_offline_mono is None
            )
        ):
            raise ValueError("active episode semantic state is inconsistent")
        engine = PhysicalEpisodeEngine(config)
        engine._episode = episode
        engine._sequence = episode.sequence
        engine.state = state
        return engine
    except (TypeError, ValueError):
        return None


def _deserialize_server(key: str, raw: object, config: DetectionConfig) -> _ServerRuntime:
    if not isinstance(raw, dict):
        raise ValueError("server record must be a mapping")
    events = []
    sanitized_events = False
    for item in raw.get("events") if isinstance(raw.get("events"), list) else []:
        try:
            event = _deserialize_event(item)
            if event.server_key != key:
                sanitized_events = True
                continue
            events.append(event)
        except Exception:
            sanitized_events = True
            continue
    events = sorted(events, key=lambda item: (item.canonical_phase_at or item.episode_started_at, item.sequence))
    unique_events = []
    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()
    seen_sequences: set[int] = set()
    for event in events:
        if event.event_id in seen_ids or event.fingerprint in seen_fingerprints:
            sanitized_events = True
            continue
        if event.sequence in seen_sequences:
            raise ValueError("conflicting persisted event sequence")
        seen_ids.add(event.event_id)
        seen_fingerprints.add(event.fingerprint)
        seen_sequences.add(event.sequence)
        unique_events.append(event)
    events = unique_events
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
            "continuity_chain_id": _optional_string(
                item.get("continuity_chain_id")
            ),
            "continuity_chain_ids": [
                value
                for value in item.get("continuity_chain_ids", ())
                if isinstance(value, str) and value
            ] if isinstance(item.get("continuity_chain_ids"), list) else [],
            "provenance_version": _nonnegative_int(
                item.get("provenance_version"), 0
            ),
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
    server.dirty = server.dirty or sanitized_events
    server.folded_through_event_seq = min(
        server.folded_through_event_seq, server.event_seq
    )
    unfolded_events = [
        item
        for item in server.events
        if item.sequence > server.folded_through_event_seq
    ]
    if len(unfolded_events) != len(server.events):
        server.events = unfolded_events
        server.routed_fingerprints = {
            item.fingerprint for item in unfolded_events
        }
        server.dirty = True
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
    restored_engine = (
        _deserialize_active_episode(key, active, config)
        if isinstance(active, dict)
        else None
    )
    if restored_engine is not None:
        server.engine = restored_engine
    elif isinstance(active, dict):
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
    diagnostic_samples = list(event.samples)
    if len(diagnostic_samples) > 48:
        diagnostic_samples = [diagnostic_samples[0], *diagnostic_samples[-47:]]
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
            [_serialize_sample(item) for item in diagnostic_samples]
            if include_samples
            else []
        ),
        "reason_codes": list(event.reason_codes),
        "app_session_id": event.app_session_id,
        "monitoring_session_id": event.monitoring_session_id,
        "poll_generation": event.poll_generation,
        "continuity_chain_id": event.continuity_chain_id,
        "provenance_version": event.provenance_version,
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
            zero_reached=_strict_bool(drain.get("zero_reached"), False),
            minimum_players=_optional_count(drain.get("minimum_players")),
            drop_fraction=_optional_bounded_float(drain.get("drop_fraction"), 0.0, 1.0),
            low_sample_count=_nonnegative_int(drain.get("low_sample_count"), 0),
            low_duration=_safe_nonnegative_float(drain.get("low_duration"), 0.0),
            abrupt=_strict_bool(drain.get("abrupt"), False),
            baseline_at=_optional_nonnegative_float(drain.get("baseline_at")),
            decline_duration=_safe_nonnegative_float(drain.get("decline_duration"), 0.0),
            inherited=_strict_bool(drain.get("inherited"), False),
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
            bounced_to_zero=_strict_bool(recovery.get("bounced_to_zero"), False),
        ),
        query_health=QueryHealthSummary(
            healthy_samples=_nonnegative_int(health.get("healthy_samples"), 0),
            failed_samples=_nonnegative_int(health.get("failed_samples"), 0),
            missing_player_samples=_nonnegative_int(health.get("missing_player_samples"), 0),
            continuous=_strict_bool(health.get("continuous"), False),
        ),
        coverage_complete=_strict_bool(raw.get("coverage_complete"), False),
        lifecycle_interruption=LifecycleMarker(lifecycle) if lifecycle in {item.value for item in LifecycleMarker} else None,
        samples=tuple(samples),
        reason_codes=tuple(str(item) for item in raw.get("reason_codes", []) if isinstance(item, str)),
        app_session_id=_optional_string(raw.get("app_session_id")),
        monitoring_session_id=_optional_string(raw.get("monitoring_session_id")),
        poll_generation=_optional_nonnegative_int(raw.get("poll_generation")),
        continuity_chain_id=_optional_string(raw.get("continuity_chain_id")),
        provenance_version=_nonnegative_int(raw.get("provenance_version"), 0),
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
        "continuity_chain_id": sample.continuity_chain_id,
        "provenance_version": sample.provenance_version,
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
        continuity_chain_id=_optional_string(raw.get("continuity_chain_id")),
        provenance_version=_nonnegative_int(raw.get("provenance_version"), 0),
    )


def _serialize_coverage(segment: CoverageSegment) -> dict:
    return {
        "start_at": segment.start_at,
        "end_at": segment.end_at,
        "kind": segment.kind.value,
        "cadence": segment.cadence,
        "server_key": segment.server_key,
        "app_session_id": segment.app_session_id,
        "monitoring_session_id": segment.monitoring_session_id,
        "poll_generation": segment.poll_generation,
        "continuity_chain_id": segment.continuity_chain_id,
        "provenance_version": segment.provenance_version,
    }


def _deserialize_coverage(raw: object) -> CoverageSegment:
    if not isinstance(raw, dict):
        raise ValueError("coverage must be a mapping")
    return CoverageSegment(
        start_at=_safe_nonnegative_float(raw.get("start_at"), 0.0),
        end_at=_safe_nonnegative_float(raw.get("end_at"), 0.0),
        kind=CoverageKind(raw.get("kind")),
        cadence=max(0.001, _safe_nonnegative_float(raw.get("cadence"), 10.0)),
        server_key=_optional_string(raw.get("server_key")),
        app_session_id=_optional_string(raw.get("app_session_id")),
        monitoring_session_id=_optional_string(raw.get("monitoring_session_id")),
        poll_generation=_optional_nonnegative_int(raw.get("poll_generation")),
        continuity_chain_id=_optional_string(raw.get("continuity_chain_id")),
        provenance_version=_nonnegative_int(raw.get("provenance_version"), 0),
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
                "compatible_multiple_support": item.compatible_multiple_support,
                "compatible_multiple_interval_count": item.compatible_multiple_interval_count,
                "skip_over_interval_count": item.skip_over_interval_count,
                "fundamental_relationship_count": item.fundamental_relationship_count,
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


def _active_episode_persistence_signature(
    episode: object,
    *,
    config: DetectionConfig,
) -> tuple[object, ...] | None:
    if episode is None:
        return None
    low_start = getattr(episode, "low_start_mono", None)
    low_samples = tuple(getattr(episode, "low_samples", ()) or ())
    low_count = _nonnegative_int(getattr(episode, "low_sample_count", 0), 0)
    low_endpoint = (
        getattr(episode, "first_player_mono", None)
        or getattr(episode, "first_queue_mono", None)
        or getattr(episode, "stable_recovery_mono", None)
        or (low_samples[-1] if low_samples else low_start)
    )
    confirmed_visible_low = bool(
        low_start is not None
        and low_endpoint is not None
        and low_endpoint - low_start >= config.low_state_minimum
        and low_count >= 3
        and any(value - low_start >= 30.0 for value in low_samples)
    )
    return (
        getattr(episode, "event_id", None),
        getattr(episode, "drain_wall", None),
        getattr(episode, "low_start_wall", None),
        getattr(episode, "zero_reached", None),
        confirmed_visible_low,
        getattr(episode, "confirmed_offline_wall", None),
        getattr(episode, "info_return_wall", None),
        getattr(episode, "first_queue_wall", None),
        getattr(episode, "first_player_wall", None),
        getattr(episode, "stable_recovery_wall", None),
        getattr(episode, "recovery_bounced", None),
        getattr(episode, "recovery_snapshot_unchanged", None),
        getattr(episode, "coverage_complete", None),
        getattr(episode, "lifecycle_interruption", None),
    )


def _expected_window_v2_episode_evidence(
    server: _ServerRuntime,
) -> tuple[ExpectedWindowEpisodeEvidence, ...]:
    values = []
    active = server.engine.active_episode
    if active is not None:
        end_at = (
            active.stable_recovery_wall
            or active.info_return_wall
            or None
        )
        values.append(
            ExpectedWindowEpisodeEvidence(
                evidence_id=active.event_id,
                start_at=active.started_wall,
                end_at=end_at,
                outage_observed=bool(
                    active.failure_count > 0
                    or active.confirmed_offline_wall is not None
                ),
                recovery_observed=bool(
                    active.info_return_wall is not None
                    or active.stable_recovery_wall is not None
                ),
                unresolved=True,
                restart_like=bool(
                    active.failure_count > 0
                    or active.drain_wall is not None
                    or active.low_sample_count > 0
                ),
                reason_codes=("runtime_active_physical_episode",),
            )
        )
    for item in server.incomplete_episodes or ():
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("event_id") or item.get("fingerprint") or "")
        started_at = _optional_nonnegative_float(item.get("started_at"))
        if not evidence_id or started_at is None:
            continue
        values.append(
            ExpectedWindowEpisodeEvidence(
                evidence_id=evidence_id,
                start_at=started_at,
                end_at=_optional_nonnegative_float(item.get("finalized_at")),
                outage_observed=False,
                recovery_observed=False,
                unresolved=True,
                restart_like=True,
                reason_codes=("persisted_incomplete_episode_diagnostic",),
            )
        )
    return tuple(values)


def _expected_window_v2_evidence_revision(
    server: _ServerRuntime,
    record: ExpectedWindowLedgerRecord,
) -> str:
    parts = [
        "expected-window-v2-shadow-evidence",
        server.key,
        str(record.candidate_period_seconds),
        str(int(round(record.expected_at))),
    ]
    parts.extend(sorted(item.event_id for item in server.events))
    parts.extend(
        sorted(
            f"{item.kind.value}:{item.start_at:.6f}:{item.end_at:.6f}"
            for item in server.coverage
            if item.end_at >= record.window_start_at
            and item.start_at <= record.window_end_at
        )
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


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


def _learning_confidence_severity(confidence: float) -> str:
    if confidence < 0.50:
        return "low"
    if confidence < 0.75:
        return "moderate"
    if confidence < 0.90:
        return "improving"
    return "strong"


def phase2_learning_summary(decision: ConsumerDecision | None, *, now: float) -> dict | None:
    if decision is None or decision.model_status is ConsumerModelStatus.NO_PATTERN:
        return None
    cycle = decision.selected_period_seconds
    prediction = decision.prediction_at if decision.prediction_usable else None
    if cycle is not None:
        hours = cycle / 3600
        unit = "hour" if hours == 1 else "hours"
        cycle_text = f"Every {hours:g} {unit}"
        confidence = decision.fundamental_period_confidence
        confidence_kind = "period"
        confidence_label = "Confidence:"
    elif decision.model_status is ConsumerModelStatus.PATTERN_OBSERVED:
        cycle_text = "Recurring timing observed"
        confidence = decision.schedule_existence_confidence
        confidence_kind = "pattern"
        confidence_label = "Pattern Confidence:"
    elif decision.model_status is ConsumerModelStatus.PERIOD_UNCERTAIN:
        cycle_text = "Period uncertain"
        confidence = decision.schedule_existence_confidence
        confidence_kind = "pattern"
        confidence_label = "Pattern Confidence:"
    elif decision.model_status is ConsumerModelStatus.SCHEDULE_CHANGE_SUSPECTED:
        cycle_text = "Schedule change suspected"
        confidence = decision.schedule_existence_confidence
        confidence_kind = "pattern"
        confidence_label = "Pattern Confidence:"
    elif decision.model_status is ConsumerModelStatus.NEW_REGIME_ESTABLISHING:
        cycle_text = "New schedule learning"
        confidence = decision.schedule_existence_confidence
        confidence_kind = "pattern"
        confidence_label = "Pattern Confidence:"
    else:
        cycle_text = "Period uncertain"
        confidence = decision.schedule_existence_confidence
        confidence_kind = "pattern"
        confidence_label = "Pattern Confidence:"
    if prediction is None:
        next_text = (
            "Period still learning"
            if cycle is None
            else "Prediction suspended"
        )
        countdown = "--"
    else:
        next_text = time.strftime("%a %H:%M", time.localtime(prediction))
        remaining = max(0, int(prediction - now))
        countdown = f"{remaining // 3600:02d}:{(remaining % 3600) // 60:02d}"
    return {
        "confidence_percent": int(round(confidence * 100)),
        "confidence_kind": confidence_kind,
        "confidence_label": confidence_label,
        "confidence_severity": _learning_confidence_severity(confidence),
        "confidence_visible": True,
        "cycle_text": cycle_text,
        "next_text": next_text,
        "next_restart_at": prediction,
        "countdown_text": countdown,
        "next_visible": cycle is None or decision.prediction_usable,
        "countdown_visible": decision.prediction_usable,
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


def _query_visible_population_transition_observable(
    server: _ServerRuntime,
    candidate: CandidateScore,
    *,
    window_start: float,
    window_end: float,
) -> bool | None:
    """Return False only when a query-visible window lacks usable population evidence."""

    if candidate.phase_offset is None:
        return None
    query_visible = {
        EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
        EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
    }
    strong_outage = {
        EventOutcome.CORROBORATED_OFFLINE_RESTART,
        EventOutcome.CONFIRMED_OFFLINE_RESTART,
    }
    aligned_query_visible = False
    for event in server.events:
        at = _event_at(event)
        if at is None:
            continue
        residual = abs(
            (
                (at - candidate.phase_offset + candidate.period_seconds / 2)
                % candidate.period_seconds
            )
            - candidate.period_seconds / 2
        )
        tolerance = candidate_phase_tolerance(candidate.period_seconds) + max(
            0.0, event.phase_uncertainty
        )
        if residual > tolerance:
            continue
        if event.outcome in strong_outage:
            return None
        if event.outcome in query_visible:
            aligned_query_visible = True
    if not aligned_query_visible:
        return None

    # A populated sample inside the completed window means a drain would have
    # been observable.  No retained samples, or only zero/near-zero samples,
    # cannot support a miss for a query-visible schedule.
    return any(
        window_start <= observed_at <= window_end and players > 1
        for observed_at, players in server.player_observations
    )


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("required string is missing")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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


def _optional_unit_float(value: object) -> float | None:
    parsed = _finite_number(value)
    return parsed if parsed is not None and 0.0 <= parsed <= 1.0 else None


def _finite_nonnegative_float_list(value: object, limit: int) -> list[float]:
    if not isinstance(value, list):
        return []
    values = []
    for item in value:
        parsed = _optional_nonnegative_float(item)
        if parsed is not None:
            values.append(parsed)
    return values[-limit:]


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


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _optional_count(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _strict_bool(value: object, default: bool) -> bool:
    return value if type(value) is bool else bool(default)


def _persisted_event_not_in_future(
    event: PhysicalRestartEvent, future_cutoff: float
) -> bool:
    timestamps = (
        event.episode_started_at,
        event.finalized_at,
        event.canonical_phase_at,
    )
    return all(value is None or value <= future_cutoff for value in timestamps)


def _minimum_optional(left: float | None, right: float | None) -> float | None:
    values = [item for item in (left, right) if item is not None]
    return min(values) if values else None


def _maximum_optional(left: float | None, right: float | None) -> float | None:
    values = [item for item in (left, right) if item is not None]
    return max(values) if values else None


def _server_activity_at(server: _ServerRuntime) -> float:
    values = [
        *(
            value
            for event in server.events
            for value in (event.finalized_at, event.canonical_phase_at)
            if value is not None
        ),
        *(segment.end_at for segment in server.coverage),
        *(
            value
            for session in server.monitoring_sessions
            for value in (session.get("started_at"), session.get("ended_at"))
            if isinstance(value, (int, float)) and math.isfinite(value)
        ),
    ]
    if server.aggregate.last_observation_at is not None:
        values.append(server.aggregate.last_observation_at)
    return max(values, default=0.0)
