"""Local internet-health evidence for Server Companion incidents.

Server Companion cannot tell a DayZ server outage from a local connectivity
outage by A2S results alone.  This module provides:

* a pure classification of independent provider probe outcomes into
  ``HEALTHY``, ``LOCAL_FAILURE`` or ``UNKNOWN``;
* a bounded, non-blocking TCP connect probe (no DNS, no payload, no
  external executables, no ICMP privileges);
* ``ServerCompanionConnectivityGuard``, a main-thread incident tracker that
  starts probing only when a qualifying server failure is observed, keeps
  probing at a bounded cadence while the failure sequence is unresolved, and
  decides whether a server failure/recovery sequence was observed with
  verified local connectivity.

Successful probes prove only that some outbound internet path works; they
never prove that the route to a particular DayZ server is healthy, and a
probe failure never implies that a server is offline.
"""

from __future__ import annotations

import errno
import logging
import math
import selectors
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Mapping


logger = logging.getLogger(__name__)


class ConnectivityStatus(str, Enum):
    HEALTHY = "healthy"
    LOCAL_FAILURE = "local_failure"
    UNKNOWN = "unknown"


class TargetOutcome(str, Enum):
    CONNECTED = "connected"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    REFUSED = "refused"
    ERROR = "error"


class ProviderResult(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    INCONCLUSIVE = "inconclusive"


class IncidentVerdict(str, Enum):
    NONE = "none"
    VERIFIED_HEALTHY = "verified_healthy"
    CONTAMINATED = "contaminated"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ProbeTarget:
    provider: str
    host: str
    port: int = 443


GOOGLE = "google"
CLOUDFLARE = "cloudflare"
REQUIRED_PROVIDERS = (GOOGLE, CLOUDFLARE)

# IP literals: no DNS resolution is performed, so a DNS-only failure can
# neither manufacture a local failure nor a healthy result.  Two anycast
# addresses per provider; one connected address is enough for that
# provider.  TCP 443 is open on all four (DNS-over-HTTPS front ends).
DEFAULT_PROBE_TARGETS = (
    ProbeTarget(GOOGLE, "8.8.8.8", 443),
    ProbeTarget(GOOGLE, "8.8.4.4", 443),
    ProbeTarget(CLOUDFLARE, "1.1.1.1", 443),
    ProbeTarget(CLOUDFLARE, "1.0.0.1", 443),
)

# One round connects to every target in parallel within this bound.
PROBE_CONNECT_TIMEOUT_SECONDS = 3.0
# While a failure sequence stays open: repeat after a healthy round, retry
# sooner after an inconclusive or failed round.
PROBE_REPEAT_AFTER_HEALTHY_SECONDS = 30.0
PROBE_RETRY_AFTER_UNRESOLVED_SECONDS = 10.0
# A recovery is verified only if the latest healthy round completed this
# recently (wall and monotonic time, so a suspend cannot hide staleness).
PROBE_FRESHNESS_SECONDS = 45.0
# An outstanding round whose result never returned is abandoned after this.
PROBE_ABANDON_SECONDS = 2 * PROBE_CONNECT_TIMEOUT_SECONDS + 4.0
# Established Server Companion continuity-gap limits (seconds).
ONLINE_OBSERVATION_GAP_SECONDS = 30.0
OFFLINE_OBSERVATION_GAP_SECONDS = 12.0

_TIMEOUT_ERRNOS = {errno.ETIMEDOUT}
_UNREACHABLE_ERRNOS = {
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    getattr(errno, "EHOSTDOWN", errno.EHOSTUNREACH),
}
_REFUSED_ERRNOS = {errno.ECONNREFUSED}
_IN_PROGRESS_ERRNOS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY}


def outcome_for_errno(code: int | None) -> TargetOutcome:
    if code in _TIMEOUT_ERRNOS:
        return TargetOutcome.TIMEOUT
    if code in _UNREACHABLE_ERRNOS:
        return TargetOutcome.UNREACHABLE
    if code in _REFUSED_ERRNOS:
        return TargetOutcome.REFUSED
    return TargetOutcome.ERROR


def classify_provider(outcomes: Iterable[TargetOutcome]) -> ProviderResult:
    """One provider: any connection succeeds; failure needs every address to
    time out or be unreachable.  Refusals and other errors are inconclusive:
    a reset may be generated locally and is not reliable evidence either way.
    """

    values = tuple(outcomes)
    if not values:
        return ProviderResult.INCONCLUSIVE
    if any(item is TargetOutcome.CONNECTED for item in values):
        return ProviderResult.SUCCESS
    if all(
        item in {TargetOutcome.TIMEOUT, TargetOutcome.UNREACHABLE} for item in values
    ):
        return ProviderResult.FAILURE
    return ProviderResult.INCONCLUSIVE


def classify_connectivity(
    providers: Mapping[str, ProviderResult],
) -> ConnectivityStatus:
    """Both independent providers must agree; anything else is UNKNOWN."""

    results = [providers.get(name) for name in REQUIRED_PROVIDERS]
    if all(item is ProviderResult.SUCCESS for item in results):
        return ConnectivityStatus.HEALTHY
    if all(item is ProviderResult.FAILURE for item in results):
        return ConnectivityStatus.LOCAL_FAILURE
    return ConnectivityStatus.UNKNOWN


def classify_target_outcomes(
    outcomes: Iterable[tuple[ProbeTarget, TargetOutcome]],
) -> tuple[ConnectivityStatus, dict[str, ProviderResult]]:
    grouped: dict[str, list[TargetOutcome]] = {}
    for target, outcome in outcomes:
        grouped.setdefault(target.provider, []).append(outcome)
    providers = {name: classify_provider(values) for name, values in grouped.items()}
    return classify_connectivity(providers), providers


def probe_targets(
    targets: Iterable[ProbeTarget] = DEFAULT_PROBE_TARGETS,
    *,
    timeout: float = PROBE_CONNECT_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[tuple[ProbeTarget, TargetOutcome], ...]:
    """Open bounded parallel non-blocking TCP connects, then close them.

    No bytes are sent.  The whole round is bounded by ``timeout``.  Must run
    off the GTK main thread.
    """

    values = tuple(targets)
    results: dict[ProbeTarget, TargetOutcome] = {}
    selector = selectors.DefaultSelector()
    sockets: list[socket.socket] = []
    try:
        for target in values:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError as exc:
                results[target] = outcome_for_errno(exc.errno)
                continue
            sockets.append(sock)
            try:
                sock.setblocking(False)
                code = sock.connect_ex((target.host, int(target.port)))
            except OSError as exc:
                code = exc.errno if exc.errno is not None else -1
            if code == 0:
                results[target] = TargetOutcome.CONNECTED
            elif code in _IN_PROGRESS_ERRNOS:
                selector.register(sock, selectors.EVENT_WRITE, target)
            else:
                results[target] = outcome_for_errno(code)
        deadline = clock() + max(0.0, float(timeout))
        while selector.get_map():
            remaining = deadline - clock()
            if remaining <= 0:
                break
            for key, _mask in selector.select(remaining):
                sock = key.fileobj
                try:
                    code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                except OSError as exc:
                    code = exc.errno if exc.errno is not None else -1
                results[key.data] = (
                    TargetOutcome.CONNECTED if code == 0 else outcome_for_errno(code)
                )
                selector.unregister(sock)
        for key in tuple(selector.get_map().values()):
            results[key.data] = TargetOutcome.TIMEOUT
    finally:
        try:
            selector.close()
        except Exception:
            pass
        for sock in sockets:
            try:
                sock.close()
            except Exception:
                pass
    return tuple((target, results.get(target, TargetOutcome.ERROR)) for target in values)


@dataclass(frozen=True)
class ProbeRound:
    status: ConnectivityStatus
    providers: Mapping[str, ProviderResult]
    completed_mono: float
    completed_wall: float


def default_probe(
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> ProbeRound:
    try:
        status, providers = classify_target_outcomes(probe_targets())
    except Exception as exc:  # never let a probe error look like evidence
        logger.debug("local connectivity probe failed: %r", exc)
        status, providers = ConnectivityStatus.UNKNOWN, {}
    return ProbeRound(status, providers, clock(), wall())


@dataclass
class ConnectivityIncident:
    incident_id: int
    scope: tuple[object, ...]
    opened_mono: float
    opened_wall: float
    last_observation_mono: float
    last_observation_wall: float
    failure_count: int = 0
    healthy_rounds: int = 0
    unknown_rounds: int = 0
    local_failure_rounds: int = 0
    last_round_status: ConnectivityStatus | None = None
    last_healthy_mono: float | None = None
    last_healthy_wall: float | None = None
    last_round_started_mono: float | None = None
    last_round_started_wall: float | None = None
    round_token: int | None = None
    unobserved_gap: bool = False
    contaminated: bool = False
    learning_suspended: bool = False
    reasons: set[str] = field(default_factory=set)


class ServerCompanionConnectivityGuard:
    """Main-thread incident tracker with bounded asynchronous probing.

    All public methods must be called on the GTK main thread.  ``submit``
    runs the probe off the main thread; ``post`` returns its result to the
    main thread.  At most one probe round is outstanding at any time, and a
    result is applied only when its token still belongs to the current open
    incident, so delayed results can never affect a later session.
    """

    def __init__(
        self,
        *,
        submit: Callable[[Callable[[], None]], object],
        post: Callable[..., object],
        probe: Callable[[], ProbeRound] = default_probe,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        on_local_failure: Callable[[ConnectivityIncident], None] | None = None,
        on_connectivity_restored: Callable[[ConnectivityIncident], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._submit = submit
        self._post = post
        self._probe = probe
        self._monotonic = monotonic
        self._wall = wall
        self.on_local_failure = on_local_failure
        self.on_connectivity_restored = on_connectivity_restored
        self._log = log or (lambda message: logger.info("%s", message))
        self._incident: ConnectivityIncident | None = None
        self._next_incident_id = 0
        self._next_token = 0
        self._outstanding_token: int | None = None
        self._outstanding_started_mono: float | None = None
        self._closed = False
        self.rounds_started = 0

    # -- state -----------------------------------------------------------

    @property
    def incident(self) -> ConnectivityIncident | None:
        return self._incident

    @property
    def probe_outstanding(self) -> bool:
        return self._outstanding_token is not None

    def learning_suspended(self, scope: tuple[object, ...]) -> bool:
        incident = self._incident
        return bool(
            incident is not None
            and incident.scope == scope
            and incident.learning_suspended
        )

    def incident_open(self, scope: tuple[object, ...] | None = None) -> bool:
        incident = self._incident
        return incident is not None and (scope is None or incident.scope == scope)

    def incident_contaminated(self) -> bool:
        incident = self._incident
        return bool(incident is not None and incident.contaminated)

    def local_failure_active(self) -> bool:
        """The latest completed round in the open incident was LOCAL_FAILURE."""

        incident = self._incident
        return bool(
            incident is not None
            and incident.last_round_status is ConnectivityStatus.LOCAL_FAILURE
        )

    # -- observations ----------------------------------------------------

    def note_failure(
        self, scope: tuple[object, ...], *, confirmed_offline: bool
    ) -> ConnectivityIncident | None:
        """A qualifying server failure was observed in ``scope``."""

        if self._closed:
            return None
        now_mono, now_wall = self._monotonic(), self._wall()
        incident = self._incident
        if incident is not None and incident.scope != scope:
            self.close_incident("scope_changed")
            incident = None
        if incident is None:
            self._next_incident_id += 1
            incident = ConnectivityIncident(
                incident_id=self._next_incident_id,
                scope=scope,
                opened_mono=now_mono,
                opened_wall=now_wall,
                last_observation_mono=now_mono,
                last_observation_wall=now_wall,
            )
            self._incident = incident
            self._log(
                f"connectivity incident opened: id={incident.incident_id}"
            )
        else:
            self._check_gap(incident, now_mono, now_wall, confirmed_offline)
        incident.failure_count += 1
        self._maybe_start_round(incident, now_mono, now_wall)
        return incident

    def note_observation(
        self, scope: tuple[object, ...], *, confirmed_offline: bool
    ) -> None:
        """A non-qualifying, non-healthy observation inside the incident."""

        incident = self._incident
        if self._closed or incident is None or incident.scope != scope:
            return
        self._check_gap(incident, self._monotonic(), self._wall(), confirmed_offline)

    def evaluate_recovery(
        self, scope: tuple[object, ...], *, confirmed_offline: bool
    ) -> IncidentVerdict:
        """Verdict for the open incident at a healthy server observation."""

        incident = self._incident
        if incident is None or incident.scope != scope:
            return IncidentVerdict.NONE
        now_mono, now_wall = self._monotonic(), self._wall()
        self._check_gap(incident, now_mono, now_wall, confirmed_offline)
        if incident.contaminated:
            return IncidentVerdict.CONTAMINATED
        reasons = []
        if incident.unobserved_gap:
            reasons.append("unobserved_polling_gap")
        if incident.unknown_rounds or incident.local_failure_rounds:
            reasons.append("inconclusive_connectivity_round")
        if incident.healthy_rounds == 0 or incident.last_healthy_mono is None:
            reasons.append("no_completed_healthy_round")
        elif incident.last_round_status is not ConnectivityStatus.HEALTHY:
            reasons.append("latest_round_not_healthy")
        else:
            age = max(
                now_mono - float(incident.last_healthy_mono),
                now_wall - float(incident.last_healthy_wall or now_wall),
            )
            if not math.isfinite(age) or age < 0 or age > PROBE_FRESHNESS_SECONDS:
                reasons.append("stale_healthy_round")
        if reasons:
            incident.reasons.update(reasons)
            return IncidentVerdict.UNRESOLVED
        return IncidentVerdict.VERIFIED_HEALTHY

    def close_incident(self, reason: str) -> None:
        incident = self._incident
        if incident is None:
            return
        self._incident = None
        # Invalidate any outstanding round for this incident; its result will
        # be ignored when it arrives.
        self._log(
            f"connectivity incident closed: id={incident.incident_id} "
            f"reason={reason} healthy_rounds={incident.healthy_rounds} "
            f"unknown_rounds={incident.unknown_rounds} "
            f"local_failure_rounds={incident.local_failure_rounds} "
            f"contaminated={incident.contaminated}"
        )

    def shutdown(self) -> None:
        self._closed = True
        self._incident = None
        self._outstanding_token = None

    # -- internals -------------------------------------------------------

    def _check_gap(
        self,
        incident: ConnectivityIncident,
        now_mono: float,
        now_wall: float,
        confirmed_offline: bool,
    ) -> None:
        limit = (
            OFFLINE_OBSERVATION_GAP_SECONDS
            if confirmed_offline
            else ONLINE_OBSERVATION_GAP_SECONDS
        )
        gap = max(
            now_mono - incident.last_observation_mono,
            now_wall - incident.last_observation_wall,
        )
        if gap > limit and not incident.unobserved_gap:
            incident.unobserved_gap = True
            incident.reasons.add("unobserved_polling_gap")
            self._log(
                f"connectivity incident observation gap: id={incident.incident_id} "
                f"gap_seconds={gap:.1f} limit={limit:.0f}"
            )
        incident.last_observation_mono = max(incident.last_observation_mono, now_mono)
        incident.last_observation_wall = max(incident.last_observation_wall, now_wall)

    def _maybe_start_round(
        self, incident: ConnectivityIncident, now_mono: float, now_wall: float
    ) -> None:
        if self._closed:
            return
        if self._outstanding_token is not None:
            started = self._outstanding_started_mono
            if started is not None and now_mono - started <= PROBE_ABANDON_SECONDS:
                return
            # The result never came back; never let that block later rounds.
            self._outstanding_token = None
            incident.reasons.add("probe_result_abandoned")
        if incident.last_round_started_mono is not None:
            interval = (
                PROBE_REPEAT_AFTER_HEALTHY_SECONDS
                if incident.last_round_status is ConnectivityStatus.HEALTHY
                else PROBE_RETRY_AFTER_UNRESOLVED_SECONDS
            )
            elapsed = max(
                now_mono - incident.last_round_started_mono,
                now_wall - float(incident.last_round_started_wall or now_wall),
            )
            if elapsed < interval:
                return
        self._next_token += 1
        token = self._next_token
        incident.round_token = token
        incident.last_round_started_mono = now_mono
        incident.last_round_started_wall = now_wall
        self._outstanding_token = token
        self._outstanding_started_mono = now_mono
        self.rounds_started += 1
        probe = self._probe
        post = self._post

        def worker() -> None:
            try:
                result = probe()
            except Exception as exc:
                logger.debug("local connectivity probe raised: %r", exc)
                result = ProbeRound(ConnectivityStatus.UNKNOWN, {}, time.monotonic(), time.time())
            try:
                post(self._apply_round, token, result)
            except Exception:
                pass

        try:
            self._submit(worker)
        except Exception as exc:
            self._outstanding_token = None
            incident.round_token = None
            incident.unknown_rounds += 1
            incident.last_round_status = ConnectivityStatus.UNKNOWN
            incident.reasons.add("probe_submit_failed")
            self._log(f"connectivity probe could not start: {exc!r}")

    def _apply_round(self, token: int, result: ProbeRound) -> bool:
        if token == self._outstanding_token:
            self._outstanding_token = None
        incident = self._incident
        if (
            self._closed
            or incident is None
            or incident.round_token != token
        ):
            return False  # stale: belongs to a closed or replaced incident
        incident.round_token = None
        status = result.status
        previous = incident.last_round_status
        incident.last_round_status = status
        if status is ConnectivityStatus.HEALTHY:
            incident.healthy_rounds += 1
            incident.last_healthy_mono = float(result.completed_mono)
            incident.last_healthy_wall = float(result.completed_wall)
        elif status is ConnectivityStatus.LOCAL_FAILURE:
            incident.local_failure_rounds += 1
        else:
            incident.unknown_rounds += 1
        if status is not previous:
            providers = ",".join(
                f"{name}={value.value}" for name, value in sorted(result.providers.items())
            )
            self._log(
                f"local connectivity {status.value}: incident={incident.incident_id} "
                f"providers=[{providers}]"
            )
        if status is ConnectivityStatus.LOCAL_FAILURE and not incident.contaminated:
            incident.contaminated = True
            incident.learning_suspended = True
            incident.reasons.add("local_connectivity_failure")
            callback = self.on_local_failure
            if callback is not None:
                callback(incident)
        elif (
            status is ConnectivityStatus.HEALTHY
            and incident.learning_suspended
        ):
            incident.learning_suspended = False
            callback = self.on_connectivity_restored
            if callback is not None:
                callback(incident)
        return False

    def resume_learning(self) -> None:
        """A healthy server observation itself proves connectivity again."""

        incident = self._incident
        if incident is not None:
            incident.learning_suspended = False
