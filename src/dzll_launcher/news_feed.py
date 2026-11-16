"""DZLL news bulletin: fetch, validate, cache, dismissal and visibility.

The bulletin is a small JSON file on the repository's ``news`` branch::

    {"revision": 3, "items": [{"type": "info", "message": "...", "url": "..."}]}

``revision`` versions the whole bulletin. A cached bulletin is replaced only
by a strictly higher remote revision. Dismissal stores the highest dismissed
revision; a bulletin is shown only while its revision is above it.

Nothing in the file is ever executed or interpreted beyond plain text. All
file and network work runs on a worker (``submit``); results return to the
GTK thread through ``schedule_idle``. This module imports no GTK.
"""

from __future__ import annotations

import json
import os
import threading
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .atomic_json import atomic_write_json
from .config import (
    NEWS_CACHE_PATH,
    NEWS_FETCH_TIMEOUT_SECS,
    NEWS_POLL_SECS,
    NEWS_STATE_PATH,
    NEWS_URL,
)

NEWS_SCHEMA = 1
NEWS_MAX_BYTES = 64 * 1024
NEWS_MAX_ITEMS = 20
NEWS_MAX_MESSAGE_CHARS = 500
NEWS_MAX_URL_CHARS = 2048
NEWS_TYPES = ("info", "warning")
NEWS_DEFAULT_TYPE = "info"
NEWS_USER_AGENT = "DZLL"


@dataclass(frozen=True)
class NewsItem:
    type: str
    message: str
    url: str | None = None


@dataclass(frozen=True)
class NewsBulletin:
    revision: int
    items: tuple[NewsItem, ...]

    def to_json(self) -> dict:
        return {
            "revision": self.revision,
            "items": [
                {"type": i.type, "message": i.message, **({"url": i.url} if i.url else {})}
                for i in self.items
            ],
        }


@dataclass(frozen=True)
class FetchResult:
    status: str  # "ok" | "not_modified" | "error"
    bulletin: NewsBulletin | None = None
    etag: str | None = None


# --- validation -------------------------------------------------------------


def _clean_text(value: str, limit: int) -> str:
    # Drop control and format characters (incl. bidi overrides), then
    # collapse all whitespace so a message is always a single line.
    kept = []
    for ch in value:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            kept.append(" " if ch in "\t\n\r\x0b\x0c" else "")
            continue
        kept.append(ch)
    text = " ".join("".join(kept).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _clean_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > NEWS_MAX_URL_CHARS or not url.lower().startswith("https://"):
        return None
    if any(unicodedata.category(ch).startswith("C") or ch.isspace() for ch in url):
        return None
    return url


def parse_bulletin(data: Any) -> NewsBulletin | None:
    """Validate decoded JSON. Returns ``None`` when the bulletin is unusable.

    Individual malformed items are skipped; an unknown ``type`` is shown as
    ``info``. An empty ``items`` list is a valid (empty) bulletin.
    """
    if not isinstance(data, dict):
        return None
    revision = data.get("revision")
    if type(revision) is not int or revision < 0:
        return None
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return None
    items: list[NewsItem] = []
    for raw in raw_items:
        if len(items) >= NEWS_MAX_ITEMS:
            break
        if not isinstance(raw, dict):
            continue
        message = raw.get("message")
        if not isinstance(message, str):
            continue
        message = _clean_text(message, NEWS_MAX_MESSAGE_CHARS)
        if not message:
            continue
        kind = raw.get("type")
        kind = kind.strip().lower() if isinstance(kind, str) else ""
        if kind not in NEWS_TYPES:
            kind = NEWS_DEFAULT_TYPE
        items.append(NewsItem(type=kind, message=message, url=_clean_url(raw.get("url"))))
    return NewsBulletin(revision=revision, items=tuple(items))


def parse_bulletin_bytes(payload: bytes) -> NewsBulletin | None:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > NEWS_MAX_BYTES:
        return None
    try:
        data = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return parse_bulletin(data)


# --- revision and visibility rules ------------------------------------------


def newer_bulletin(cached: NewsBulletin | None, remote: NewsBulletin | None) -> NewsBulletin | None:
    """The bulletin to keep: the remote one only if its revision is higher."""
    if remote is None:
        return cached
    if cached is None or remote.revision > cached.revision:
        return remote
    return cached


def is_dismissed(bulletin: NewsBulletin | None, dismissed_revision: int | None) -> bool:
    return (
        bulletin is not None
        and dismissed_revision is not None
        and bulletin.revision <= dismissed_revision
    )


def visible_items(
    bulletin: NewsBulletin | None,
    dismissed_revision: int | None,
    hidden_by_setting: bool,
) -> tuple[NewsItem, ...]:
    """Items to show, or an empty tuple when the news section is hidden."""
    if hidden_by_setting or bulletin is None or not bulletin.items:
        return ()
    if is_dismissed(bulletin, dismissed_revision):
        return ()
    return bulletin.items


# --- local persistence ------------------------------------------------------


def _read_json_file(path: str) -> Any:
    try:
        with open(path, "rb") as f:
            raw = f.read(NEWS_MAX_BYTES * 2 + 1)
        if len(raw) > NEWS_MAX_BYTES * 2:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None


def load_cache(path: str = NEWS_CACHE_PATH) -> tuple[NewsBulletin | None, str | None]:
    data = _read_json_file(path)
    if not isinstance(data, dict) or data.get("schema") != NEWS_SCHEMA:
        return None, None
    bulletin = parse_bulletin(data.get("bulletin"))
    etag = data.get("etag")
    if bulletin is None:
        return None, None
    return bulletin, (etag if isinstance(etag, str) and etag else None)


def save_cache(bulletin: NewsBulletin, etag: str | None, path: str = NEWS_CACHE_PATH) -> None:
    atomic_write_json(
        path,
        {"schema": NEWS_SCHEMA, "etag": etag or None, "bulletin": bulletin.to_json()},
        indent=2,
    )


def load_dismissed_revision(path: str = NEWS_STATE_PATH) -> int | None:
    data = _read_json_file(path)
    if not isinstance(data, dict):
        return None
    value = data.get("dismissed_revision")
    return value if type(value) is int and value >= 0 else None


def save_dismissed_revision(revision: int, path: str = NEWS_STATE_PATH) -> None:
    atomic_write_json(
        path, {"schema": NEWS_SCHEMA, "dismissed_revision": int(revision)}, indent=2
    )


# --- network ----------------------------------------------------------------


def fetch_bulletin(
    etag: str | None = None,
    *,
    url: str = NEWS_URL,
    timeout: float = NEWS_FETCH_TIMEOUT_SECS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> FetchResult:
    """Download and validate the bulletin. Never raises."""
    headers = {"User-Agent": NEWS_USER_AGENT, "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        req = urllib.request.Request(url, headers=headers)
        with opener(req, timeout=timeout) as resp:
            payload = resp.read(NEWS_MAX_BYTES + 1)
            new_etag = resp.headers.get("ETag") if getattr(resp, "headers", None) else None
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult("not_modified", etag=etag)
        return FetchResult("error")
    except Exception:
        return FetchResult("error")
    bulletin = parse_bulletin_bytes(payload)
    if bulletin is None:
        return FetchResult("error")
    return FetchResult("ok", bulletin=bulletin, etag=new_etag if isinstance(new_etag, str) else None)


def refresh_cache_from_remote(
    fetch: Callable[[str | None], FetchResult],
    cache_path: str = NEWS_CACHE_PATH,
) -> NewsBulletin | None:
    """Worker step: fetch, keep the higher revision, persist. Returns the bulletin to use.

    Invalid responses and failures leave the cache untouched.
    """
    cached, etag = load_cache(cache_path)
    # Without a usable cache, a 304 would leave nothing to show.
    result = fetch(etag if cached is not None else None)
    if result.status != "ok" or result.bulletin is None:
        return cached
    keep = newer_bulletin(cached, result.bulletin)
    if keep is result.bulletin and (cached is None or result.bulletin.revision > cached.revision):
        save_cache(result.bulletin, result.etag, cache_path)
    elif cached is not None and result.bulletin.revision == cached.revision and result.etag != etag:
        # Same bulletin, new validator: retain the cached content.
        save_cache(cached, result.etag, cache_path)
    return keep


# --- controller (GTK thread) ------------------------------------------------


class NewsFeedController:
    """Owns news state on the GTK thread; does all I/O through ``submit``.

    ``on_changed`` runs on the GTK thread whenever ``items`` may have changed.
    """

    def __init__(
        self,
        *,
        is_hidden: Callable[[], bool],
        submit: Callable[[Callable[[], None]], Any],
        schedule_idle: Callable[..., Any],
        timeout_add_seconds: Callable[..., int],
        source_remove: Callable[[int], Any],
        on_changed: Callable[[], None],
        fetch: Callable[[str | None], FetchResult] = fetch_bulletin,
        cache_path: str = NEWS_CACHE_PATH,
        state_path: str = NEWS_STATE_PATH,
        poll_secs: int = NEWS_POLL_SECS,
    ) -> None:
        self._is_hidden = is_hidden
        self._submit = submit
        self._schedule_idle = schedule_idle
        self._timeout_add_seconds = timeout_add_seconds
        self._source_remove = source_remove
        self._on_changed = on_changed
        self._fetch = fetch
        self._cache_path = cache_path
        self._state_path = state_path
        self._poll_secs = int(poll_secs)

        self.bulletin: NewsBulletin | None = None
        self.dismissed_revision: int | None = None
        self._local_loaded = False
        self._started = False
        self._closed = False
        self._fetch_inflight = False
        self._poll_source_id = 0
        self._persist_lock = threading.Lock()

    # -- queries --

    @property
    def items(self) -> tuple[NewsItem, ...]:
        return visible_items(self.bulletin, self.dismissed_revision, self._hidden())

    @property
    def poll_active(self) -> bool:
        return bool(self._poll_source_id)

    @property
    def fetch_inflight(self) -> bool:
        return self._fetch_inflight

    # -- lifecycle --

    def start(self) -> None:
        """Load the local cache and dismissal, then check GitHub if enabled."""
        if self._started or self._closed:
            return
        self._started = True
        fetch_now = not self._hidden()
        if fetch_now:
            self._fetch_inflight = True
            self._start_poll()
        self._run_worker(fetch_now, load_local=True)

    def set_hidden(self, hidden: bool) -> None:
        """Apply the Hide News Ticker setting. Dismissal state is untouched."""
        if self._closed:
            return
        if hidden:
            self._stop_poll()
        elif self._started:
            self._start_poll()
            self.refresh()
        self._notify()

    def refresh(self) -> bool:
        """Check GitHub now. Skipped while hidden, closed or already in flight."""
        if self._closed or not self._started or self._hidden() or self._fetch_inflight:
            return False
        self._fetch_inflight = True
        self._run_worker(True, load_local=not self._local_loaded)
        return True

    def dismiss(self) -> bool:
        """Dismiss the whole current bulletin, permanently, by revision."""
        if self._closed or self.bulletin is None:
            return False
        revision = self.bulletin.revision
        if self.dismissed_revision is not None and self.dismissed_revision >= revision:
            self._notify()
            return False
        self.dismissed_revision = revision
        self._notify()
        state_path = self._state_path
        lock = self._persist_lock

        def worker():
            with lock:
                # Never lower a dismissal written earlier (e.g. by a race).
                current = load_dismissed_revision(state_path)
                if current is None or current < revision:
                    try:
                        save_dismissed_revision(revision, state_path)
                    except Exception:
                        pass

        self._safe_submit(worker)
        return True

    def shutdown(self) -> None:
        self._closed = True
        self._stop_poll()

    # -- internals --

    def _hidden(self) -> bool:
        try:
            return bool(self._is_hidden())
        except Exception:
            return False

    def _run_worker(self, fetch_remote: bool, *, load_local: bool) -> None:
        cache_path = self._cache_path
        state_path = self._state_path
        fetch = self._fetch
        lock = self._persist_lock

        def worker():
            if load_local:
                cached, _etag = load_cache(cache_path)
                with lock:
                    dismissed = load_dismissed_revision(state_path)
                self._schedule_idle(self._apply_local, cached, dismissed)
            if fetch_remote:
                try:
                    bulletin = refresh_cache_from_remote(fetch, cache_path)
                except Exception:
                    bulletin = None
                self._schedule_idle(self._apply_remote, bulletin)

        if not self._safe_submit(worker):
            self._fetch_inflight = False

    def _safe_submit(self, worker) -> bool:
        try:
            self._submit(worker)
            return True
        except Exception:
            return False

    def _apply_local(self, cached: NewsBulletin | None, dismissed: int | None) -> bool:
        if self._closed:
            return False
        self._local_loaded = True
        self.bulletin = newer_bulletin(self.bulletin, cached)
        # Keep the higher of the persisted and any in-session dismissal.
        if dismissed is not None and (self.dismissed_revision is None or dismissed > self.dismissed_revision):
            self.dismissed_revision = dismissed
        self._notify()
        return False

    def _apply_remote(self, bulletin: NewsBulletin | None) -> bool:
        self._fetch_inflight = False
        if self._closed:
            return False
        self.bulletin = newer_bulletin(self.bulletin, bulletin)
        self._notify()
        return False

    def _poll_tick(self) -> bool:
        if self._closed or self._hidden():
            self._poll_source_id = 0
            return False
        self.refresh()
        return True

    def _start_poll(self) -> None:
        if self._poll_source_id or self._closed:
            return
        try:
            self._poll_source_id = int(self._timeout_add_seconds(self._poll_secs, self._poll_tick) or 0)
        except Exception:
            self._poll_source_id = 0

    def _stop_poll(self) -> None:
        source_id, self._poll_source_id = self._poll_source_id, 0
        if source_id:
            try:
                self._source_remove(source_id)
            except Exception:
                pass

    def _notify(self) -> None:
        try:
            self._on_changed()
        except Exception:
            pass
