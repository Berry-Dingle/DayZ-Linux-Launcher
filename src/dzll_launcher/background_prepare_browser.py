"""Passive main-loop bridge for browser preparation presentation."""

from __future__ import annotations

import threading

from .preparation_contracts import PreparationOutcome, PreparationProgressEvent


class BrowserPreparationPresenter:
    """Forward immutable authoritative values to one current browser operation."""

    def __init__(self, *, generation, schedule, is_current, reducer_factory,
                 render_snapshot,
                 render_cancelling, render_terminal):
        self.generation = int(generation)
        self._schedule = schedule
        self._is_current = is_current
        self._reducer_factory = reducer_factory
        self._reducer = None
        self._render_snapshot = render_snapshot
        self._render_cancelling = render_cancelling
        self._render_terminal = render_terminal
        self._lock = threading.Lock()
        self._terminal_scheduled = False

    def _dispatch(self, callback, value):
        generation = self.generation

        def deliver():
            if self._is_current(generation):
                callback(value)
            return False

        self._schedule(deliver)

    def on_event(self, event: PreparationProgressEvent) -> None:
        generation = self.generation

        def reduce_and_render():
            if not self._is_current(generation):
                return False
            if self._reducer is None:
                self._reducer = self._reducer_factory(int(event.operation_id))
            reduction = self._reducer.apply(event)
            if reduction.snapshot is not None:
                self._render_snapshot(reduction.snapshot)
            return False

        self._schedule(reduce_and_render)

    def on_cancelling(self, operation_id: int) -> None:
        self._dispatch(self._render_cancelling, int(operation_id))

    def on_steamcmd_state(self, *, heading: str, line1: str, line2: str,
                          spinning: bool) -> None:
        """Receive semantic state from the existing SteamCMD overlay parser."""
        generation = self.generation

        def reduce_and_render():
            if not self._is_current(generation) or self._reducer is None:
                return False
            reduction = self._reducer.apply_steamcmd_state(
                heading=heading, line1=line1, line2=line2, spinning=spinning,
            )
            if reduction.snapshot is not None:
                self._render_snapshot(reduction.snapshot)
            return False

        self._schedule(reduce_and_render)

    def on_terminal(self, outcome: PreparationOutcome) -> None:
        with self._lock:
            if self._terminal_scheduled:
                return
            self._terminal_scheduled = True
        self._dispatch(self._render_terminal, outcome)
