"""GitHub latest-release checks for the update notice.

These functions do the network and settings work and run on worker threads.
The window applies their results on the GTK thread.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable

from .config import APP_VERSION, GITHUB_LATEST_API, RELEASES_URL

UPDATE_CHECK_INTERVAL_SECONDS = 86400


def _fetch_latest_release() -> tuple[str, str]:
    """Return ``(tag, url)`` for the latest GitHub release. Raises on any failure."""
    req = urllib.request.Request(GITHUB_LATEST_API, headers={"User-Agent": "DZLL"})
    with urllib.request.urlopen(req, timeout=3.0) as r:
        raw = r.read().decode("utf-8", "replace")
    data = json.loads(raw)
    tag = str(data.get("tag_name") or "").strip()
    url = str(data.get("html_url") or RELEASES_URL).strip()
    return tag, url


def _record_release(settings: dict, save_settings: Callable[[dict], None], *, now: int, tag: str, url: str) -> None:
    try:
        settings["last_update_check_ts"] = now
        settings["latest_release_tag"] = tag
        settings["latest_release_url"] = url
        save_settings(settings)
    except Exception:
        pass


def startup_update_info(settings: dict, *, save_settings: Callable[[dict], None]) -> dict | None:
    """Automatic startup check, run before the server database fetch.

    Returns the ``{"tag", "url"}`` update info to publish, or ``None`` to leave
    the current value unchanged. A successful network check made more than a
    day after the last one is recorded in ``settings`` and saved.
    """
    if not bool(settings.get("auto_check_updates", True)):
        return None

    test_tag = os.environ.get("DZLL_TEST_UPDATE_TAG", "").strip()
    if test_tag:
        test_url = os.environ.get("DZLL_TEST_UPDATE_URL", "").strip() or RELEASES_URL
        return {"tag": test_tag, "url": test_url}

    forced = os.environ.get("DZLL_FORCE_LATEST_TAG", "").strip()
    if forced:
        return {"tag": forced, "url": RELEASES_URL}

    now = int(time.time())
    last = int(settings.get("last_update_check_ts", 0) or 0)

    if last and (now - last) < UPDATE_CHECK_INTERVAL_SECONDS:
        tag = str(settings.get("latest_release_tag") or "").strip()
        url = str(settings.get("latest_release_url") or RELEASES_URL).strip()
        if tag:
            return {"tag": tag, "url": url}
        return None

    try:
        tag, url = _fetch_latest_release()
    except Exception:
        tag = ""
        url = ""

    _record_release(settings, save_settings, now=now, tag=tag, url=url)

    if tag:
        return {"tag": tag, "url": url}
    return None


def manual_update_check(settings: dict, *, save_settings: Callable[[dict], None]) -> tuple[str, str, str]:
    """User-requested check from Settings.

    Returns ``(state, tag, url)``, where state is one of ``available``,
    ``latest``, ``no_stable_release`` or ``failed``.
    """
    test_tag = os.environ.get("DZLL_TEST_UPDATE_TAG", "").strip()
    if test_tag:
        test_url = os.environ.get("DZLL_TEST_UPDATE_URL", "").strip() or RELEASES_URL
        return "available", test_tag, test_url

    try:
        tag, url = _fetch_latest_release()
    except urllib.error.HTTPError as e:
        if getattr(e, "code", None) == 404:
            return "no_stable_release", "", ""
        return "failed", "", ""
    except Exception:
        return "failed", "", ""

    _record_release(settings, save_settings, now=int(time.time()), tag=tag, url=url)

    if tag and str(APP_VERSION).strip() not in ("", "dev") and tag != str(APP_VERSION).strip():
        return "available", tag, url
    return "latest", "", ""
