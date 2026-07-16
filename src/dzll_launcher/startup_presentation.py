from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .companion_restart_phase2_runtime import RuntimeNotice


@dataclass(frozen=True)
class StartupPresentationResult:
    completed_now: bool
    update_shown: bool
    notice_shown: bool


class StartupPresentationCoordinator:
    """Small lifecycle coordinator for startup/update/reset presentation.

    GTK visibility is supplied by callbacks.  This keeps ordering deterministic
    and makes every shown/not-shown/failure path testable without a main loop.
    """

    def __init__(
        self,
        *,
        pending_notice: RuntimeNotice | None,
        hide_startup: Callable[[], None],
        maybe_show_update: Callable[[], bool],
        blockers_visible: Callable[[], bool],
        show_notice: Callable[[RuntimeNotice], bool],
    ) -> None:
        self.pending_notice = pending_notice
        self._hide_startup = hide_startup
        self._maybe_show_update = maybe_show_update
        self._blockers_visible = blockers_visible
        self._show_notice = show_notice
        self.startup_completed = False
        self.update_visible = False
        self.notice_showing = False
        self.shutdown_begun = False
        self._update_attempted = False

    def complete_startup(self) -> StartupPresentationResult:
        if self.startup_completed:
            shown = self.try_show_notice()
            return StartupPresentationResult(False, self.update_visible, shown)
        self._hide_startup()
        self.startup_completed = True
        update_shown = False
        if not self._update_attempted:
            self._update_attempted = True
            try:
                update_shown = bool(self._maybe_show_update())
            except Exception:
                update_shown = False
        self.update_visible = update_shown
        notice_shown = self.try_show_notice()
        return StartupPresentationResult(True, update_shown, notice_shown)

    def update_visibility_changed(self, visible: bool) -> bool:
        self.update_visible = bool(visible)
        return self.try_show_notice()

    def blocker_visibility_changed(self) -> bool:
        return self.try_show_notice()

    def notice_dismissed(self) -> None:
        self.notice_showing = False

    def begin_shutdown(self) -> None:
        self.shutdown_begun = True

    def try_show_notice(self) -> bool:
        if (
            self.pending_notice is None
            or self.notice_showing
            or not self.startup_completed
            or self.update_visible
            or self.shutdown_begun
        ):
            return False
        try:
            if self._blockers_visible():
                return False
        except Exception:
            return False
        try:
            shown = bool(self._show_notice(self.pending_notice))
        except Exception:
            return False
        if not shown:
            return False
        self.notice_showing = True
        self.pending_notice = None
        return True
