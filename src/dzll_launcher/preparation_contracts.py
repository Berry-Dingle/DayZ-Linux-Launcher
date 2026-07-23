"""Passive contracts for the existing required-mod preparation flow.

These types deliberately contain no scheduling, polling, readiness, retry, or
current-item selection behaviour.  They carry outcomes and authoritative
events produced by the existing Join preparation path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


class PreparationStatus(Enum):
    READY = "ready"
    NO_REQUIRED_MODS = "no_required_mods"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class PreparationOutcome:
    status: PreparationStatus
    reason: str = ""
    error: str = ""
    backend: str = ""
    effective_workshop_path: str = ""
    verified_mods: tuple[tuple[int, str], ...] = ()
    did_work: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "verified_mods",
            tuple((int(mod_id), str(name or "")) for mod_id, name in self.verified_mods),
        )


class PreparationEventKind(Enum):
    STAGE = "stage"
    ITEM = "item"
    ITEM_COMPLETED = "item_completed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _freeze(value):
    if isinstance(value, dict):
        return ("dict", tuple((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_freeze(item) for item in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_freeze(item) for item in value))
    if isinstance(value, set):
        return ("set", tuple(sorted((_freeze(item) for item in value), key=repr)))
    return value


def _thaw(value):
    if isinstance(value, tuple) and len(value) == 2 and value[0] in {
        "dict", "list", "tuple", "set",
    }:
        kind, items = value
        if kind == "dict":
            return {key: _thaw(item) for key, item in items}
        values = [_thaw(item) for item in items]
        if kind == "tuple":
            return tuple(values)
        if kind == "set":
            return set(values)
        return values
    return value


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PreparationProgressEvent:
    kind: PreparationEventKind
    operation_id: int = 0
    backend: str = ""
    backend_owner: str = ""
    item_id: int = 0
    item_name: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0
    fraction: float | None = None
    indeterminate: bool | None = None
    payload: tuple = ()

    def __post_init__(self) -> None:
        payload = self.payload
        already_frozen = (
            isinstance(payload, tuple) and len(payload) == 2
            and payload[0] in {"dict", "list", "tuple", "set"}
        )
        if not already_frozen and payload:
            object.__setattr__(self, "payload", _freeze(payload))

    @classmethod
    def from_authoritative_payload(cls, payload: dict) -> "PreparationProgressEvent":
        source = dict(payload or {})
        raw_type = str(source.get("type") or "")
        operation_id = _as_int(source.get("join_attempt_id"))
        item_id = _as_int(source.get("id"))
        if raw_type in ("cancelled", "cancelling") or bool(source.get("cancelled", False)):
            kind = PreparationEventKind.CANCELLED
        elif raw_type == "error" or bool(source.get("error", False)):
            kind = PreparationEventKind.FAILED
        elif raw_type == "done":
            kind = PreparationEventKind.COMPLETED
        elif item_id > 0 and bool(source.get("ready", False)):
            kind = PreparationEventKind.ITEM_COMPLETED
        elif item_id > 0:
            kind = PreparationEventKind.ITEM
        else:
            kind = PreparationEventKind.STAGE
        return cls(
            kind=kind,
            operation_id=operation_id,
            backend=str(source.get("backend") or ""),
            backend_owner=str(source.get("backend_owner") or ""),
            item_id=item_id,
            item_name=str(source.get("name") or ""),
            downloaded_bytes=_as_int(source.get("download_bytes")),
            total_bytes=_as_int(source.get("total_bytes")),
            fraction=_as_optional_float(source.get("fraction")),
            indeterminate=(
                bool(source["indeterminate"])
                if source.get("indeterminate") is not None else None
            ),
            payload=_freeze(source),
        )

    def authoritative_payload(self) -> dict:
        payload = _thaw(self.payload)
        return dict(payload) if isinstance(payload, dict) else {}


class PreparationPresenter(Protocol):
    """Output-only observer.  Preparation decisions are intentionally absent."""

    def on_event(self, event: PreparationProgressEvent) -> None: ...

    def on_cancelling(self, operation_id: int) -> None: ...

    def on_terminal(self, outcome: PreparationOutcome) -> None: ...


class NoOpPreparationPresenter:
    def on_event(self, event: PreparationProgressEvent) -> None:
        return None

    def on_cancelling(self, operation_id: int) -> None:
        return None

    def on_terminal(self, outcome: PreparationOutcome) -> None:
        return None


class RecordingPreparationPresenter:
    """Non-GTK recorder which preserves immutable authoritative values verbatim."""

    def __init__(self):
        self._events: list[PreparationProgressEvent] = []
        self._cancelling_operation_ids: list[int] = []
        self._terminal: PreparationOutcome | None = None

    @property
    def events(self) -> tuple[PreparationProgressEvent, ...]:
        return tuple(self._events)

    @property
    def cancelling_operation_ids(self) -> tuple[int, ...]:
        return tuple(self._cancelling_operation_ids)

    @property
    def terminal(self) -> PreparationOutcome | None:
        return self._terminal

    def on_event(self, event: PreparationProgressEvent) -> None:
        self._events.append(event)

    def on_cancelling(self, operation_id: int) -> None:
        self._cancelling_operation_ids.append(int(operation_id))

    def on_terminal(self, outcome: PreparationOutcome) -> None:
        if self._terminal is None:
            self._terminal = outcome


class JoinPopupPreparationPresenter:
    """Compatibility adapter to the established Join popup event consumer."""

    def __init__(self, *, consume_event: Callable[[dict], object],
                 consume_cancelling: Callable[[int], object] | None = None,
                 consume_terminal: Callable[[PreparationOutcome], object] | None = None):
        self._consume_event = consume_event
        self._consume_cancelling = consume_cancelling
        self._consume_terminal = consume_terminal
        self._terminal_delivered = False

    def on_event(self, event: PreparationProgressEvent) -> None:
        self._consume_event(event.authoritative_payload())

    def on_cancelling(self, operation_id: int) -> None:
        if self._consume_cancelling is not None:
            self._consume_cancelling(int(operation_id))

    def on_terminal(self, outcome: PreparationOutcome) -> None:
        if self._terminal_delivered:
            return
        self._terminal_delivered = True
        if self._consume_terminal is not None:
            self._consume_terminal(outcome)
