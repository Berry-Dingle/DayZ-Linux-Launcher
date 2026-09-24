"""Single-server caller for the one shared required-mod preparation engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading

from .join_prepare import prepare_required_mods
from .mod_metadata import clean_display_mod_name
from .preparation_contracts import (
    NoOpPreparationPresenter,
    PreparationOutcome,
    PreparationPresenter,
    PreparationStatus,
)
from .server_endpoint import normalize_server_endpoint
from .steam_ugc_backend import UGCHelperReapError


_GATE_CREATION_LOCK = threading.Lock()


@dataclass(frozen=True)
class BackgroundServerPreparationSnapshot:
    ip: str
    game_port: int
    query_port: int
    name: str
    required_mods: tuple[tuple[int, str], ...]

    @property
    def identity(self) -> str:
        return f"{self.ip}:{self.game_port}"

    @classmethod
    def from_server(cls, server, required_mods):
        ip, game_port, query_port = normalize_server_endpoint(
            getattr(server, "ip", None),
            getattr(server, "gport", None),
            getattr(server, "qport", None),
        )
        return cls(
            ip=ip,
            game_port=game_port,
            query_port=query_port,
            name=str(getattr(server, "name", "") or ""),
            required_mods=tuple(
                (int(mod_id), clean_display_mod_name(name, mod_id, fallback=False))
                for mod_id, name in (required_mods or ())
            ),
        )


@dataclass(frozen=True)
class BackgroundPreparationRuntime:
    workshop_dir: str
    mod_management_enabled: bool
    auto_install_missing: bool


class BackgroundConsentStatus(Enum):
    ALLOWED = "allowed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    ERROR = "error"
    STALE = "stale"


@dataclass(frozen=True)
class BackgroundConsentResult:
    status: BackgroundConsentStatus
    error: str = ""
    steam_was_running: bool = False
    steam_start_submitted: bool = False
    backend_may_launch: bool = False

    @property
    def allowed(self) -> bool:
        return self.status is BackgroundConsentStatus.ALLOWED

    @property
    def startup_state(self) -> str:
        if self.steam_was_running:
            return "already_running"
        if self.steam_start_submitted:
            return "start_submitted"
        if self.backend_may_launch:
            return "backend_allowed"
        return self.status.value

    @property
    def startup_confirmed(self) -> bool:
        return bool(
            self.steam_was_running
            or self.steam_start_submitted
            or self.backend_may_launch
        )

    def __bool__(self) -> bool:
        return self.allowed


def _coerce_background_consent_result(value) -> BackgroundConsentResult:
    if isinstance(value, BackgroundConsentResult):
        return value
    return BackgroundConsentResult(
        BackgroundConsentStatus.ALLOWED if bool(value) else BackgroundConsentStatus.DECLINED,
        backend_may_launch=bool(value),
    )


@dataclass(frozen=True)
class _OperationLease:
    generation: int
    owner: str


class PreparationOperationState(Enum):
    IDLE = "idle"
    ACTIVE = "active"
    BLOCKED_REAP_FAILURE = "blocked_reap_failure"


@dataclass(frozen=True)
class PreparationReapFailure:
    lease: _OperationLease
    error: UGCHelperReapError

    @property
    def reason(self) -> str:
        return str(self.error or "Steam UGC helper shutdown could not be confirmed.")


class PreparationOperationGate:
    """Small process-local serialization gate; it schedules no work."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._active: _OperationLease | None = None
        self._reap_failure: PreparationReapFailure | None = None

    @property
    def state(self) -> PreparationOperationState:
        with self._lock:
            if self._reap_failure is not None:
                return PreparationOperationState.BLOCKED_REAP_FAILURE
            if self._active is not None:
                return PreparationOperationState.ACTIVE
            return PreparationOperationState.IDLE

    @property
    def blocked_reap_failure(self) -> bool:
        return self.state is PreparationOperationState.BLOCKED_REAP_FAILURE

    @property
    def blocked_reason(self) -> str:
        with self._lock:
            failure = self._reap_failure
        return failure.reason if failure is not None else ""

    @property
    def blocked_generation(self) -> int:
        with self._lock:
            failure = self._reap_failure
        return int(failure.lease.generation) if failure is not None else 0

    @property
    def active_owner(self) -> str:
        with self._lock:
            return self._active.owner if self._active is not None else ""

    def try_acquire(self, owner: str) -> _OperationLease | None:
        with self._lock:
            if self._active is not None:
                return None
            self._generation += 1
            self._active = _OperationLease(self._generation, str(owner))
            return self._active

    def owns(self, lease: _OperationLease) -> bool:
        with self._lock:
            return self._active == lease

    def release(self, lease: _OperationLease) -> bool:
        with self._lock:
            if self._active != lease or self._reap_failure is not None:
                return False
            self._active = None
            return True

    def mark_reap_failure(
        self,
        lease: _OperationLease,
        error: UGCHelperReapError,
    ) -> bool:
        with self._lock:
            if self._active != lease:
                return False
            self._reap_failure = PreparationReapFailure(lease, error)
            return True

    def block_reap_failure(
        self,
        owner: str,
        error: UGCHelperReapError,
    ) -> bool:
        """Poison an otherwise idle gate for an unresolved foreground helper."""

        with self._lock:
            if self._reap_failure is not None:
                return True
            if self._active is not None:
                return False
            self._generation += 1
            lease = _OperationLease(self._generation, str(owner))
            self._active = lease
            self._reap_failure = PreparationReapFailure(lease, error)
            return True

    def try_recover_reap_failure(self, *, expected_generation: int = 0) -> bool:
        """Clear poison only after the retained Process confirms its own exit."""

        with self._lock:
            failure = self._reap_failure
        if failure is None:
            return False
        if (
            int(expected_generation or 0)
            and failure.lease.generation != int(expected_generation)
        ):
            return False
        if not failure.error.finalize_confirmed_recovery():
            return False
        with self._lock:
            if self._reap_failure != failure or self._active != failure.lease:
                return False
            self._reap_failure = None
            self._active = None
            return True


def preparation_operation_gate(owner) -> PreparationOperationGate:
    gate = getattr(owner, "_preparation_operation_gate", None)
    if gate is None:
        with _GATE_CREATION_LOCK:
            gate = getattr(owner, "_preparation_operation_gate", None)
            if gate is None:
                gate = PreparationOperationGate()
                setattr(owner, "_preparation_operation_gate", gate)
    return gate


def preparation_operation_busy(owner) -> bool:
    return bool(preparation_operation_gate(owner).active_owner)


class SingleServerBackgroundPreparation:
    """Invoke shared preparation once, then stop at its terminal outcome."""

    def __init__(self, win):
        self._win = win
        self._gate = preparation_operation_gate(win)
        self._state_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._lease: _OperationLease | None = None
        self._presenter: PreparationPresenter | None = None
        self._cancel_requested = False

    @property
    def active(self) -> bool:
        with self._state_lock:
            lease = self._lease
        return lease is not None and self._gate.owns(lease)

    @property
    def cancel_event(self):
        return self._cancel_event

    def cancel(self) -> bool:
        with self._state_lock:
            lease = self._lease
            presenter = self._presenter
            if lease is None:
                self._cancel_requested = True
                return True
        if not self._gate.owns(lease):
            return False
        if presenter is not None:
            presenter.on_cancelling(lease.generation)
        self._cancel_event.set()
        return True

    def run(self, snapshot: BackgroundServerPreparationSnapshot,
            runtime: BackgroundPreparationRuntime,
            presenter: PreparationPresenter | None = None,
            ensure_steam_consent=None) -> PreparationOutcome:
        presenter = presenter or NoOpPreparationPresenter()
        join_active = getattr(getattr(self._win, "_join_attempts", None), "active", None)
        lease = None if join_active is not None else self._gate.try_acquire("background")
        if lease is None:
            outcome = PreparationOutcome(
                PreparationStatus.FAILED,
                reason="preparation_busy",
                error="Another mod preparation operation is already active.",
            )
            presenter.on_terminal(outcome)
            return outcome

        with self._state_lock:
            self._lease = lease
            self._presenter = presenter
            cancel_requested = self._cancel_requested

        cancel_event = self._cancel_event
        cancel_event.clear()
        helper_reap_blocked = False
        if cancel_requested:
            cancel_event.set()
        try:
            if cancel_requested:
                outcome = PreparationOutcome(
                    PreparationStatus.CANCELLED,
                    reason="cancelled_before_start",
                    error="Background preparation was cancelled before it started.",
                    backend="steam_client",
                )
                presenter.on_terminal(outcome)
                return outcome
            consent = ensure_steam_consent or (
                lambda: self._win._ensure_join_steam_start_consent(0)
            )
            if snapshot.required_mods and not cancel_requested:
                consent_result = _coerce_background_consent_result(consent())
                if not consent_result.allowed:
                    status = consent_result.status
                    failed = status in {
                        BackgroundConsentStatus.TIMEOUT,
                        BackgroundConsentStatus.ERROR,
                    }
                    reason = {
                        BackgroundConsentStatus.DECLINED: "steam_start_declined",
                        BackgroundConsentStatus.CANCELLED: "consent_cancelled",
                        BackgroundConsentStatus.TIMEOUT: "steam_start_timeout",
                        BackgroundConsentStatus.ERROR: "steam_start_error",
                        BackgroundConsentStatus.STALE: "consent_stale",
                    }.get(status, "steam_start_declined")
                    default_error = {
                        BackgroundConsentStatus.DECLINED: "Steam start was declined.",
                        BackgroundConsentStatus.CANCELLED: "Steam start permission was cancelled.",
                        BackgroundConsentStatus.TIMEOUT: (
                            f"Timed out waiting for Steam start permission for {snapshot.name or snapshot.identity}."
                        ),
                        BackgroundConsentStatus.ERROR: (
                            f"Could not obtain Steam start permission for {snapshot.name or snapshot.identity}."
                        ),
                        BackgroundConsentStatus.STALE: (
                            f"Steam start permission expired for {snapshot.name or snapshot.identity}."
                        ),
                    }.get(status, "Steam start was declined.")
                    outcome = PreparationOutcome(
                        PreparationStatus.FAILED if failed else PreparationStatus.CANCELLED,
                        reason=reason,
                        error=str(consent_result.error or default_error),
                        backend="steam_client",
                    )
                    presenter.on_terminal(outcome)
                    return outcome
                if not consent_result.startup_confirmed:
                    outcome = PreparationOutcome(
                        PreparationStatus.FAILED,
                        reason="steam_start_not_submitted",
                        error=(
                            "Steam start permission completed without confirming that "
                            "native Steam was running or that startup was submitted."
                        ),
                        backend="steam_client",
                    )
                    presenter.on_terminal(outcome)
                    return outcome

            return prepare_required_mods(
                self._win,
                snapshot.required_mods,
                runtime.workshop_dir,
                runtime.mod_management_enabled,
                runtime.auto_install_missing,
                operation_id=lease.generation,
                presenter=presenter,
                server_name=snapshot.name,
                server_identity=snapshot.identity,
                is_operation_current=lambda: self._gate.owns(lease),
                manage_join_presence=False,
                manage_join_presentation=False,
                allow_backend_steam_start=bool(
                    consent_result.backend_may_launch
                    if snapshot.required_mods else False
                ),
                cancel_event=cancel_event,
            )
        except UGCHelperReapError as exc:
            helper_reap_blocked = bool(exc.helper_process_may_be_alive)
            if helper_reap_blocked:
                self._gate.mark_reap_failure(lease, exc)
            raise
        finally:
            cancel_event.clear()
            self._win._join_steam_start_allowed = False
            if not helper_reap_blocked:
                with self._state_lock:
                    self._lease = None
                    self._presenter = None
                self._gate.release(lease)


def prepare_server_mods_without_joining(
        win, snapshot, runtime, presenter=None, ensure_steam_consent=None,
        controller=None):
    operation = controller or SingleServerBackgroundPreparation(win)
    return operation.run(
        snapshot, runtime, presenter, ensure_steam_consent,
    )
