from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any
import weakref


SCROLLBAR_FACTORY_NAMES = (
    "fav",
    "name",
    "time",
    "played",
    "map",
    "players",
    "ping",
    "monitor",
    "join",
)

_ENV_TRUE_VALUES = frozenset(("1", "true", "yes", "on"))
_ENV_FALSE_VALUES = frozenset(("0", "false", "no", "off"))


def drag_light_enabled_from_env(value: str | None) -> bool:
    """Parse the troubleshooting override, defaulting safely to enabled."""
    if value is None:
        return True
    normalized = str(value).strip().lower()
    if normalized in _ENV_TRUE_VALUES:
        return True
    if normalized in _ENV_FALSE_VALUES:
        return False
    return True


@dataclass
class FactoryLifecycleMetrics:
    setup_count: int = 0
    bind_count: int = 0
    unbind_count: int = 0
    bind_total_ns: int = 0
    bind_max_ns: int = 0
    unbind_total_ns: int = 0
    unbind_max_ns: int = 0
    light_bind_count: int = 0
    full_bind_count: int = 0
    light_bind_total_ns: int = 0
    light_bind_max_ns: int = 0


@dataclass(frozen=True)
class ScrollbarFactoryMetricsSnapshot:
    generation: int
    factories: dict[str, FactoryLifecycleMetrics]
    operations: dict[str, int]
    settle_refresh_count: int
    settle_refresh_total_ns: int
    settle_refresh_max_ns: int
    settle_refresh_max_factory: str


class ScrollbarFactoryMetrics:
    """Generation-scoped metrics with a zero-timing disabled path."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.active = False
        self.generation = 0
        self.factories: dict[str, FactoryLifecycleMetrics] = {}
        self.operations: dict[str, int] = {}
        self.settle_refresh_count = 0
        self.settle_refresh_total_ns = 0
        self.settle_refresh_max_ns = 0
        self.settle_refresh_max_factory = "-"

    def begin(self, generation: int) -> bool:
        if not self.enabled:
            return False
        self.generation = int(generation)
        self.active = True
        self.factories = {
            name: FactoryLifecycleMetrics() for name in SCROLLBAR_FACTORY_NAMES
        }
        self.operations = {}
        self.settle_refresh_count = 0
        self.settle_refresh_total_ns = 0
        self.settle_refresh_max_ns = 0
        self.settle_refresh_max_factory = "-"
        return True

    def record_setup(self, factory_name: str) -> bool:
        if not self.active:
            return False
        metrics = self.factories.get(factory_name)
        if metrics is None:
            return False
        metrics.setup_count += 1
        return True

    def start(self, factory_name: str, operation: str, *, bind_mode: str = "full"):
        if not self.active or operation not in ("bind", "unbind"):
            return None
        if factory_name not in self.factories:
            return None
        return (
            self.generation,
            factory_name,
            operation,
            bind_mode,
            time.perf_counter_ns(),
        )

    def finish(self, token) -> bool:
        if token is None or not self.active:
            return False
        generation, factory_name, operation, bind_mode, started_ns = token
        if int(generation) != self.generation:
            return False
        metrics = self.factories.get(factory_name)
        if metrics is None:
            return False
        duration_ns = max(0, time.perf_counter_ns() - int(started_ns))
        if operation == "bind":
            metrics.bind_count += 1
            metrics.bind_total_ns += duration_ns
            metrics.bind_max_ns = max(metrics.bind_max_ns, duration_ns)
            if bind_mode == "light":
                metrics.light_bind_count += 1
                metrics.light_bind_total_ns += duration_ns
                metrics.light_bind_max_ns = max(
                    metrics.light_bind_max_ns,
                    duration_ns,
                )
            else:
                metrics.full_bind_count += 1
        elif operation == "unbind":
            metrics.unbind_count += 1
            metrics.unbind_total_ns += duration_ns
            metrics.unbind_max_ns = max(metrics.unbind_max_ns, duration_ns)
        else:
            return False
        return True

    def start_settle_refresh(self, factory_name: str):
        if not self.active or factory_name not in self.factories:
            return None
        return (self.generation, factory_name, time.perf_counter_ns())

    def finish_settle_refresh(self, token, *, applied: bool = True) -> bool:
        if token is None or not self.active or not applied:
            return False
        generation, factory_name, started_ns = token
        if int(generation) != self.generation or factory_name not in self.factories:
            return False
        duration_ns = max(0, time.perf_counter_ns() - int(started_ns))
        self.settle_refresh_count += 1
        self.settle_refresh_total_ns += duration_ns
        if duration_ns > self.settle_refresh_max_ns:
            self.settle_refresh_max_ns = duration_ns
            self.settle_refresh_max_factory = factory_name
        self.count(f"{factory_name}_settle_cells_refreshed")
        return True

    def count(self, operation: str, amount: int = 1) -> bool:
        if not self.active:
            return False
        name = str(operation)
        self.operations[name] = int(self.operations.get(name, 0)) + int(amount)
        return True

    def settle(self, generation: int) -> ScrollbarFactoryMetricsSnapshot | None:
        if not self.active or int(generation) != self.generation:
            return None
        self.active = False
        factories = {
            name: FactoryLifecycleMetrics(**vars(metrics))
            for name, metrics in self.factories.items()
        }
        return ScrollbarFactoryMetricsSnapshot(
            generation=self.generation,
            factories=factories,
            operations=dict(self.operations),
            settle_refresh_count=self.settle_refresh_count,
            settle_refresh_total_ns=self.settle_refresh_total_ns,
            settle_refresh_max_ns=self.settle_refresh_max_ns,
            settle_refresh_max_factory=self.settle_refresh_max_factory,
        )


@dataclass(frozen=True)
class _BoundCellEntry:
    key: int
    serial: int
    factory_name: str
    cell: Any
    list_item: Any
    current_obj: Any
    generation: int
    full_refresh_ref: weakref.ReferenceType


class BoundCellRegistry:
    """Bounded strong cell records removed explicitly by factory unbind."""

    def __init__(self) -> None:
        self._entries: dict[int, _BoundCellEntry] = {}
        self._next_serial = 0

    @staticmethod
    def _weak_callable(callback):
        try:
            if getattr(callback, "__self__", None) is not None:
                return weakref.WeakMethod(callback)
            return weakref.ref(callback)
        except TypeError:
            return None

    def register(
        self,
        factory_name: str,
        cell,
        list_item,
        current_obj,
        full_refresh,
        *,
        generation: int,
    ) -> bool:
        if factory_name not in SCROLLBAR_FACTORY_NAMES or not callable(full_refresh):
            return False
        refresh_ref = self._weak_callable(full_refresh)
        if refresh_ref is None:
            return False
        self._next_serial += 1
        key = id(cell)
        self._entries[key] = _BoundCellEntry(
            key=key,
            serial=self._next_serial,
            factory_name=factory_name,
            cell=cell,
            list_item=list_item,
            current_obj=current_obj,
            generation=int(generation),
            full_refresh_ref=refresh_ref,
        )
        return True

    def unregister(self, cell, list_item=None) -> bool:
        key = id(cell)
        entry = self._entries.get(key)
        if entry is None or entry.cell is not cell:
            return False
        if list_item is not None and entry.list_item is not list_item:
            return False
        del self._entries[key]
        return True

    def snapshot(self) -> list[tuple[int, int]]:
        return [(key, entry.serial) for key, entry in self._entries.items()]

    def resolve(self, key: int) -> _BoundCellEntry | None:
        return self._entries.get(int(key))

    def remove_if_current(self, key: int, serial: int) -> bool:
        entry = self._entries.get(int(key))
        if entry is None or entry.serial != int(serial):
            return False
        del self._entries[int(key)]
        return True

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class ScrollbarDragLightController:
    """Generation-scoped light-bind gate with explicit bound-cell lifecycle."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.active = False
        self.generation = 0
        self.registry = BoundCellRegistry()
        self.refresh_pending = False

    def begin(self, generation: int) -> bool:
        if not self.enabled or self.active:
            return False
        self.generation = int(generation)
        self.active = True
        self.refresh_pending = True
        return True

    def is_active(self) -> bool:
        return self.enabled and self.active

    def deactivate(self, generation: int) -> bool:
        if not self.active or int(generation) != self.generation:
            return False
        self.active = False
        return True

    def register(
        self,
        factory_name: str,
        cell,
        list_item,
        current_obj,
        full_refresh,
    ) -> bool:
        if not self.enabled:
            return False
        return self.registry.register(
            factory_name,
            cell,
            list_item,
            current_obj,
            full_refresh,
            generation=self.generation if self.active else 0,
        )

    def unregister(self, cell, list_item=None) -> bool:
        if not self.enabled:
            return False
        return self.registry.unregister(cell, list_item)

    def clear(self) -> None:
        self.registry.clear()

    def refresh_bound_cells(self, perf_metrics=None) -> dict[str, int]:
        if not self.enabled or self.active or not self.refresh_pending:
            return {}
        self.refresh_pending = False
        counts: dict[str, int] = {}
        snapshot = self.registry.snapshot()
        registry_before = len(snapshot)
        registry_live = 0
        registry_stale = 0
        if perf_metrics is not None and perf_metrics.active:
            perf_metrics.count("settle_full_refresh_passes")
            perf_metrics.count("registry_before_settle", registry_before)
        for key, snapshot_serial in snapshot:
            entry = self.registry.resolve(key)
            if entry is None:
                registry_stale += 1
                continue
            if entry.generation != self.generation:
                registry_stale += 1
                continue
            full_refresh = entry.full_refresh_ref()
            if full_refresh is None:
                registry_stale += 1
                self.registry.remove_if_current(key, entry.serial)
                continue
            try:
                child = entry.list_item.get_child()
                current_obj = entry.list_item.get_item()
            except Exception:
                registry_stale += 1
                self.registry.remove_if_current(key, entry.serial)
                continue
            if child is not entry.cell or current_obj is not entry.current_obj:
                registry_stale += 1
                # A newer registration must win over this snapshot. Only prune
                # the exact stale record that was inspected.
                if entry.serial == snapshot_serial:
                    self.registry.remove_if_current(key, entry.serial)
                continue
            registry_live += 1
            token = (
                perf_metrics.start_settle_refresh(entry.factory_name)
                if perf_metrics is not None
                else None
            )
            applied = False
            try:
                full_refresh(entry.list_item, entry.cell)
                current_entry = self.registry.resolve(key)
                current_item = entry.list_item.get_item()
                applied = bool(
                    current_entry is not None
                    and current_entry.serial == entry.serial
                    and current_item is entry.current_obj
                )
                if applied:
                    counts[entry.factory_name] = counts.get(entry.factory_name, 0) + 1
            except Exception:
                pass
            finally:
                if perf_metrics is not None:
                    perf_metrics.finish_settle_refresh(token, applied=applied)
        if perf_metrics is not None and perf_metrics.active:
            perf_metrics.count("registry_live", registry_live)
            perf_metrics.count("registry_stale", registry_stale)
            perf_metrics.count("registry_after_settle", len(self.registry))
        return counts


@dataclass(frozen=True)
class ScrollbarInteractionSettlement:
    generation: int
    reason: str
    started_at: float
    settled_at: float
    first_adjustment_value: float | None
    latest_adjustment_value: float | None
    adjustment_events: int
    deferred_requests: int
    coalesced_requests: int
    deferred_work: dict[str, Any]
    last_adjustment_time: float | None

    @property
    def duration(self) -> float:
        return max(0.0, self.settled_at - self.started_at)

    @property
    def last_adjustment_to_settle(self) -> float | None:
        if self.last_adjustment_time is None:
            return None
        return max(0.0, self.settled_at - self.last_adjustment_time)


@dataclass
class ScrollbarInteractionState:
    """GTK-independent latest-request state for one scrollbar interaction."""

    active: bool = False
    generation: int = 0
    latest_adjustment_value: float | None = None
    first_adjustment_value: float | None = None
    adjustment_events: int = 0
    last_adjustment_time: float | None = None
    deferred_requests: int = 0
    coalesced_requests: int = 0
    interaction_start_time: float = 0.0
    settle_time: float = 0.0
    settle_reason: str = ""
    settle_pending: bool = False
    _deferred_work: dict[str, Any] = field(default_factory=dict, repr=False)

    def begin(self, *, now: float, adjustment_value: float | None = None) -> tuple[int, bool]:
        if self.active:
            return self.generation, False
        self.generation += 1
        self.active = True
        self.first_adjustment_value = adjustment_value
        self.latest_adjustment_value = adjustment_value
        self.adjustment_events = 0
        self.last_adjustment_time = None
        self.deferred_requests = 0
        self.coalesced_requests = 0
        self.interaction_start_time = float(now)
        self.settle_time = 0.0
        self.settle_reason = ""
        self.settle_pending = False
        self._deferred_work.clear()
        return self.generation, True

    def is_current(self, generation: int) -> bool:
        return self.active and int(generation) == self.generation

    def record_adjustment(
        self,
        value: float,
        *,
        generation: int | None = None,
        now: float | None = None,
    ) -> bool:
        if not self.active or (generation is not None and not self.is_current(generation)):
            return False
        value = float(value)
        if self.adjustment_events == 0 and self.first_adjustment_value is None:
            self.first_adjustment_value = value
        self.latest_adjustment_value = value
        self.adjustment_events += 1
        if now is not None:
            self.last_adjustment_time = float(now)
        return True

    def defer(self, category: str, work: Any, *, generation: int | None = None) -> bool:
        if not self.active or (generation is not None and not self.is_current(generation)):
            return False
        name = str(category)
        self.deferred_requests += 1
        if name in self._deferred_work:
            self.coalesced_requests += 1
        self._deferred_work[name] = work
        return True

    def request_settle(self, *, generation: int | None = None) -> bool:
        if not self.active or self.settle_pending:
            return False
        if generation is not None and not self.is_current(generation):
            return False
        self.settle_pending = True
        return True

    def settle(
        self,
        *,
        reason: str,
        now: float,
        generation: int | None = None,
    ) -> ScrollbarInteractionSettlement | None:
        if not self.active or (generation is not None and not self.is_current(generation)):
            return None
        settled_at = float(now)
        settlement = ScrollbarInteractionSettlement(
            generation=self.generation,
            reason=str(reason or "unknown"),
            started_at=self.interaction_start_time,
            settled_at=settled_at,
            first_adjustment_value=self.first_adjustment_value,
            latest_adjustment_value=self.latest_adjustment_value,
            adjustment_events=self.adjustment_events,
            deferred_requests=self.deferred_requests,
            coalesced_requests=self.coalesced_requests,
            deferred_work=dict(self._deferred_work),
            last_adjustment_time=self.last_adjustment_time,
        )
        # Clear active before callers drain any deferred work.
        self.active = False
        self.settle_pending = False
        self.settle_time = settled_at
        self.settle_reason = settlement.reason
        self._deferred_work.clear()
        return settlement

    def cancel(
        self,
        *,
        reason: str,
        now: float,
        generation: int | None = None,
    ) -> ScrollbarInteractionSettlement | None:
        return self.settle(reason=reason, now=now, generation=generation)
