"""Steam's current DayZ player count, shown in the title bar."""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable

from .config import STEAM_CURRENT_PLAYERS_URL


def fetch_steam_global_player_count() -> int | None:
    """Return Steam's current DayZ player count, or ``None`` on any failure."""
    try:
        req = urllib.request.Request(
            STEAM_CURRENT_PLAYERS_URL,
            headers={"User-Agent": "DZLL"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as r:
            raw = r.read().decode("utf-8", "replace")
        data = json.loads(raw)
        resp = data.get("response") if isinstance(data, dict) else None
        if isinstance(resp, dict):
            pc = resp.get("player_count")
            return int(pc)
    except Exception:
        pass
    return None


class SteamGlobalPlayersPoller:
    """Fetches the player count off the GTK thread and delivers it back to it.

    The window owns the GLib timer sources and calls ``tick``. The fetch runs
    through ``submit`` (a worker executor). ``schedule_idle`` returns the result
    to the GTK thread, where ``count`` is updated and ``on_updated`` runs.
    """

    def __init__(
        self,
        *,
        settings: Callable[[], dict],
        is_shut_down: Callable[[], bool],
        submit: Callable[[Callable[[], None]], Any],
        schedule_idle: Callable[..., Any],
        on_updated: Callable[[], None],
        fetch: Callable[[], int | None] = fetch_steam_global_player_count,
    ) -> None:
        self._settings = settings
        self._is_shut_down = is_shut_down
        self._submit = submit
        self._schedule_idle = schedule_idle
        self._on_updated = on_updated
        self._fetch = fetch
        self.count: int | None = None

    def tick(self) -> bool:
        """GLib timeout callback. Returns ``False`` only after shutdown."""
        if self._is_shut_down():
            return False
        settings = self._settings()
        if not bool(settings.get("show_counts_in_title_bar", False)):
            return True
        if not bool(settings.get("show_counts_global_players", True)):
            return True

        def worker():
            val = self._fetch()
            self._schedule_idle(self._apply, val)

        try:
            self._submit(worker)
        except Exception:
            pass

        return True

    def _apply(self, val) -> bool:
        if isinstance(val, int) and val >= 0:
            self.count = val
        try:
            self._on_updated()
        except Exception:
            pass
        return False
