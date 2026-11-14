from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import window
from dzll_launcher.window import DZLLWindow

LOCAL_ROWS = [{"ip": "1.1.1.1", "gport": 2302, "qport": 27016, "source": "local"}]
FRESH_ROWS = [{"ip": "2.2.2.2", "gport": 2302, "qport": 27016, "source": "fresh"}]


class Loop:
    """Records GLib sources so each test decides the order in which they run."""

    def __init__(self):
        self.timeouts = {}
        self.idles = []

    def timeout_add(self, interval, callback, *args):
        self.timeouts[("ms", interval)] = (callback, args)
        return len(self.timeouts)

    def timeout_add_seconds(self, interval, callback, *args):
        self.timeouts[("s", interval)] = (callback, args)
        return len(self.timeouts)

    def idle_add(self, callback, *args):
        self.idles.append((callback, args))
        return len(self.idles)

    def fire(self, key):
        callback, args = self.timeouts.pop(key)
        return callback(*args)

    def run_idles(self):
        while self.idles:
            callback, args = self.idles.pop(0)
            callback(*args)


class SyncThread:
    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def startup(monkeypatch):
    loop = Loop()
    for name in ("timeout_add", "timeout_add_seconds", "idle_add"):
        monkeypatch.setattr(window.GLib, name, getattr(loop, name))
    monkeypatch.setattr(window.threading, "Thread", SyncThread)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)

    db_state = {"fresh_available": False, "local_reads": 0}

    def read_servers():
        if db_state["fresh_available"]:
            return list(FRESH_ROWS)
        db_state["local_reads"] += 1
        return list(LOCAL_ROWS)

    def fetch():
        db_state["fresh_available"] = True
        return True

    monkeypatch.setattr(window, "read_servers_from_db", read_servers)
    monkeypatch.setattr(window, "fetch_db_overwrite_local", fetch)

    workers = []
    applied = []

    def apply_db_rows(self, rows, fetched_ok, **_kwargs):
        applied.append((rows[0]["source"], fetched_ok))
        return False

    host = SimpleNamespace(
        _set_updating=lambda *_args, **_kwargs: None,
        empty_label=SimpleNamespace(set_visible=lambda _visible: None),
        _db_executor=SimpleNamespace(submit=workers.append),
        _check_updates_worker=lambda: None,
        _complete_startup_presentation=lambda: None,
        _startup_perf_origin_ns=0,
    )
    host._apply_db_rows = MethodType(apply_db_rows, host)
    host._begin_startup_update = MethodType(DZLLWindow._begin_startup_update, host)
    host._begin_startup_update()
    assert len(workers) == 1
    return SimpleNamespace(
        host=host, loop=loop, worker=workers[0], applied=applied, db_state=db_state,
        fallback_key=("ms", window.STARTUP_LOCAL_DB_FALLBACK_MS),
    )


def test_slow_download_shows_local_copy_then_fresh_database(startup):
    startup.loop.fire(startup.fallback_key)
    startup.loop.run_idles()
    assert startup.applied == [("local", False)]
    assert startup.host._startup_db_provisional is True

    startup.worker()
    startup.loop.run_idles()
    assert startup.applied == [("local", False), ("fresh", True)]
    assert startup.host._startup_db_applied is True


def test_fast_download_never_loads_the_local_copy(startup):
    startup.worker()
    startup.loop.run_idles()
    startup.loop.fire(startup.fallback_key)
    startup.loop.run_idles()

    assert startup.applied == [("fresh", True)]
    assert startup.db_state["local_reads"] == 0


def test_fresh_database_arriving_during_local_read_wins(startup):
    startup.loop.fire(startup.fallback_key)
    startup.worker()
    # The fresh result is applied before the queued local-copy callback runs.
    startup.loop.idles.reverse()
    startup.loop.run_idles()

    assert startup.applied == [("fresh", True)]


def test_watchdog_skips_rereading_after_local_copy_is_shown(startup):
    startup.loop.fire(startup.fallback_key)
    startup.loop.run_idles()
    reads = startup.db_state["local_reads"]

    startup.loop.fire(("s", 30))

    assert startup.db_state["local_reads"] == reads
    assert startup.applied == [("local", False)]
