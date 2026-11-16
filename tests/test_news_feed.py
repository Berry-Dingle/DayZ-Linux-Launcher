"""News bulletin: validation, revision rules, cache, dismissal and controller."""

import io
import json
import threading
import urllib.error

import pytest

from dzll_launcher import news_feed as nf
from dzll_launcher.news_feed import FetchResult, NewsBulletin, NewsFeedController, NewsItem


def _bulletin(revision, *messages, kind="info"):
    return NewsBulletin(revision=revision, items=tuple(NewsItem(kind, m) for m in messages))


def _payload(revision, *messages):
    return {"revision": revision, "items": [{"type": "info", "message": m} for m in messages]}


# --- validation ----------------------------------------------------------------


def test_parses_the_documented_example():
    data = {
        "revision": 1,
        "items": [
            {"type": "info", "message": "Welcome to the DZLL news ticker!"},
            {"type": "warning", "message": "This is a test warning announcement."},
            {
                "type": "info",
                "message": "Visit GitHub for DZLL releases.",
                "url": "https://github.com/Berry-Dingle/DayZ-Linux-Launcher/releases",
            },
        ],
    }
    bulletin = nf.parse_bulletin(data)
    assert bulletin.revision == 1
    assert [i.type for i in bulletin.items] == ["info", "warning", "info"]
    assert bulletin.items[2].url.startswith("https://github.com/")


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "text",
        {"items": []},
        {"revision": "2", "items": []},
        {"revision": True, "items": []},
        {"revision": -1, "items": []},
        {"revision": 1.5, "items": []},
        {"revision": 1},
        {"revision": 1, "items": {"message": "x"}},
    ],
)
def test_invalid_bulletins_are_rejected(data):
    assert nf.parse_bulletin(data) is None


@pytest.mark.parametrize("payload", [b"{not json", b"\xff\xfe", b"[1, 2]", b"", b"{}" + b" " * nf.NEWS_MAX_BYTES])
def test_invalid_bytes_are_rejected(payload):
    assert nf.parse_bulletin_bytes(payload) is None


def test_empty_items_is_a_valid_empty_bulletin():
    bulletin = nf.parse_bulletin({"revision": 4, "items": []})
    assert bulletin == NewsBulletin(4, ())
    assert nf.visible_items(bulletin, None, False) == ()


def test_unsupported_and_malformed_items_are_handled_safely():
    bulletin = nf.parse_bulletin(
        {
            "revision": 2,
            "items": [
                {"type": "critical", "message": "Unknown type"},
                {"type": 7, "message": "Numeric type"},
                {"message": "No type"},
                {"type": "info"},
                {"type": "info", "message": "   "},
                {"type": "info", "message": 42},
                "string item",
                None,
                {"type": "WARNING", "message": "Upper"},
            ],
        }
    )
    assert [(i.type, i.message) for i in bulletin.items] == [
        ("info", "Unknown type"),
        ("info", "Numeric type"),
        ("info", "No type"),
        ("warning", "Upper"),
    ]


def test_text_is_sanitised_to_one_plain_line():
    bulletin = nf.parse_bulletin(
        {
            "revision": 1,
            "items": [
                {"message": "Line one\nline\ttwo\x00\x1b[31m red‮"},
                {"message": "x" * (nf.NEWS_MAX_MESSAGE_CHARS + 50)},
                {"message": "<b>not markup</b> & stays literal"},
            ],
        }
    )
    assert bulletin.items[0].message == "Line one line two[31m red"
    assert len(bulletin.items[1].message) == nf.NEWS_MAX_MESSAGE_CHARS
    assert bulletin.items[2].message == "<b>not markup</b> & stays literal"


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://github.com/x", True),
        ("http://example.com", False),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
        ("steam://run/221100", False),
        ("https://a b", False),
        (5, False),
    ],
)
def test_only_https_urls_are_kept(url, kept):
    bulletin = nf.parse_bulletin({"revision": 1, "items": [{"message": "m", "url": url}]})
    assert (bulletin.items[0].url is not None) is kept


def test_item_count_is_capped():
    bulletin = nf.parse_bulletin(_payload(1, *[f"m{i}" for i in range(nf.NEWS_MAX_ITEMS + 5)]))
    assert len(bulletin.items) == nf.NEWS_MAX_ITEMS


# --- revision and visibility rules ---------------------------------------------


def test_revision_comparison_and_replacement():
    old, new = _bulletin(1, "old"), _bulletin(2, "new")
    assert nf.newer_bulletin(None, old) is old
    assert nf.newer_bulletin(old, new) is new
    assert nf.newer_bulletin(new, old) is new  # never downgrade
    same = _bulletin(2, "different text, same revision")
    assert nf.newer_bulletin(new, same) is new  # unchanged revision keeps the cache
    assert nf.newer_bulletin(new, None) is new


def test_same_revision_stays_dismissed_and_higher_revision_returns():
    assert nf.visible_items(_bulletin(3, "a"), 3, False) == ()
    assert nf.visible_items(_bulletin(2, "a"), 3, False) == ()
    assert nf.visible_items(_bulletin(4, "a"), 3, False) == (NewsItem("info", "a"),)


def test_hide_setting_overrides_everything():
    assert nf.visible_items(_bulletin(9, "a"), None, True) == ()
    assert nf.visible_items(None, None, False) == ()


# --- persistence -----------------------------------------------------------------


def test_cache_round_trip_and_corruption(tmp_path):
    path = str(tmp_path / "news_cache.json")
    assert nf.load_cache(path) == (None, None)
    nf.save_cache(_bulletin(5, "hello"), '"etag-1"', path)
    assert nf.load_cache(path) == (_bulletin(5, "hello"), '"etag-1"')
    (tmp_path / "news_cache.json").write_text("{broken", encoding="utf-8")
    assert nf.load_cache(path) == (None, None)
    (tmp_path / "news_cache.json").write_text(json.dumps({"schema": 1, "bulletin": {"revision": "x"}}), encoding="utf-8")
    assert nf.load_cache(path) == (None, None)


def test_dismissal_round_trip_and_corruption(tmp_path):
    path = str(tmp_path / "news_state.json")
    assert nf.load_dismissed_revision(path) is None
    nf.save_dismissed_revision(7, path)
    assert nf.load_dismissed_revision(path) == 7
    (tmp_path / "news_state.json").write_text('{"dismissed_revision": "7"}', encoding="utf-8")
    assert nf.load_dismissed_revision(path) is None


# --- network ----------------------------------------------------------------------


class _Resp:
    def __init__(self, body, etag=None):
        self._body = body
        self.headers = {"ETag": etag} if etag else {}

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_fetch_sends_etag_and_handles_not_modified():
    seen = {}

    def opener(req, timeout):
        seen["etag"] = req.get_header("If-none-match")
        seen["timeout"] = timeout
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, io.BytesIO(b""))

    result = nf.fetch_bulletin('"abc"', opener=opener)
    assert result == FetchResult("not_modified", etag='"abc"')
    assert seen == {"etag": '"abc"', "timeout": nf.NEWS_FETCH_TIMEOUT_SECS}


def test_fetch_ok_returns_bulletin_and_etag():
    body = json.dumps(_payload(3, "hi")).encode()
    result = nf.fetch_bulletin(None, opener=lambda req, timeout: _Resp(body, '"e3"'))
    assert result.status == "ok"
    assert result.bulletin == _bulletin(3, "hi")
    assert result.etag == '"e3"'


@pytest.mark.parametrize(
    "opener",
    [
        lambda req, timeout: (_ for _ in ()).throw(TimeoutError("slow")),
        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        lambda req, timeout: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b""))
        ),
        lambda req, timeout: _Resp(b"<html>rate limited</html>"),
        lambda req, timeout: _Resp(b"x" * (nf.NEWS_MAX_BYTES + 10)),
    ],
)
def test_fetch_failures_never_raise(opener):
    assert nf.fetch_bulletin(None, opener=opener).status == "error"


def test_refresh_replaces_only_on_higher_revision(tmp_path):
    path = str(tmp_path / "c.json")
    nf.save_cache(_bulletin(5, "cached"), '"e5"', path)

    # Older remote: cache kept.
    assert nf.refresh_cache_from_remote(lambda etag: FetchResult("ok", _bulletin(4, "older"), '"e4"'), path) == _bulletin(5, "cached")
    assert nf.load_cache(path)[0] == _bulletin(5, "cached")
    # Same revision with different text: cache content kept.
    assert nf.refresh_cache_from_remote(lambda etag: FetchResult("ok", _bulletin(5, "changed"), '"e5b"'), path) == _bulletin(5, "cached")
    assert nf.load_cache(path) == (_bulletin(5, "cached"), '"e5b"')
    # Newer remote: replaced.
    assert nf.refresh_cache_from_remote(lambda etag: FetchResult("ok", _bulletin(6, "new"), '"e6"'), path) == _bulletin(6, "new")
    assert nf.load_cache(path) == (_bulletin(6, "new"), '"e6"')


def test_refresh_keeps_cache_on_failure_and_invalid_responses(tmp_path):
    path = str(tmp_path / "c.json")
    nf.save_cache(_bulletin(2, "cached"), '"e2"', path)
    before = (tmp_path / "c.json").read_bytes()
    for result in (FetchResult("error"), FetchResult("not_modified", etag='"e2"')):
        assert nf.refresh_cache_from_remote(lambda etag, r=result: r, path) == _bulletin(2, "cached")
    assert (tmp_path / "c.json").read_bytes() == before


def test_refresh_without_cache_does_not_send_a_stale_etag(tmp_path):
    seen = []
    nf.refresh_cache_from_remote(lambda etag: seen.append(etag) or FetchResult("error"), str(tmp_path / "none.json"))
    assert seen == [None]


# --- controller --------------------------------------------------------------------


class _Loop:
    """Synchronous stand-ins for executor submit, idle and GLib timers."""

    def __init__(self):
        self.jobs = []
        self.idle = []
        self.timers = {}
        self._next = 1
        self.threads = set()
        self.submit_raises = False

    def submit(self, fn):
        if self.submit_raises:
            raise RuntimeError("executor shut down")
        self.jobs.append(fn)

    def schedule_idle(self, fn, *args):
        self.idle.append((fn, args))

    def timeout_add_seconds(self, secs, fn):
        sid = self._next
        self._next += 1
        self.timers[sid] = (secs, fn)
        return sid

    def source_remove(self, sid):
        self.timers.pop(sid, None)

    def run(self):
        while self.jobs or self.idle:
            while self.jobs:
                job = self.jobs.pop(0)
                t = threading.Thread(target=lambda: (self.threads.add(threading.get_ident()), job()))
                t.start()
                t.join()
            while self.idle:
                fn, args = self.idle.pop(0)
                fn(*args)


class _Env:
    def __init__(self, tmp_path, *, hidden=False, remote=None):
        self.loop = _Loop()
        self.settings = {"hide_news_ticker": hidden}
        self.remote = remote
        self.fetches = []
        self.changes = 0
        self.ui_threads = set()
        self.cache_path = str(tmp_path / "cache" / "news_cache.json")
        self.state_path = str(tmp_path / "config" / "news_state.json")
        self.controller = self.new_controller()

    def fetch(self, etag):
        self.fetches.append(etag)
        if self.remote is None:
            return FetchResult("error")
        if isinstance(self.remote, FetchResult):
            return self.remote
        return FetchResult("ok", self.remote, None)

    def on_changed(self):
        self.changes += 1
        self.ui_threads.add(threading.get_ident())

    def new_controller(self):
        return NewsFeedController(
            is_hidden=lambda: bool(self.settings["hide_news_ticker"]),
            submit=self.loop.submit,
            schedule_idle=self.loop.schedule_idle,
            timeout_add_seconds=self.loop.timeout_add_seconds,
            source_remove=self.loop.source_remove,
            on_changed=self.on_changed,
            fetch=self.fetch,
            cache_path=self.cache_path,
            state_path=self.state_path,
            poll_secs=2700,
        )

    def restart(self):
        """Simulate quitting and relaunching DZLL."""
        self.controller.shutdown()
        self.loop = _Loop()
        self.controller = self.new_controller()
        self.controller.start()
        self.loop.run()
        return self.controller


def test_start_does_no_io_on_the_calling_thread(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(1, "hello"))
    env.controller.start()
    # Nothing happened synchronously: all I/O is queued for a worker.
    assert env.fetches == [] and env.controller.items == () and len(env.loop.jobs) == 1
    env.loop.run()
    assert env.controller.items == (NewsItem("info", "hello"),)
    main = threading.get_ident()
    assert main not in env.loop.threads  # fetch and file reads ran on workers
    assert env.ui_threads == {main}  # UI notifications came back on the caller's thread


def test_cached_bulletin_is_used_when_the_network_fails(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(3, "first"))
    env.controller.start()
    env.loop.run()
    env.remote = None  # offline
    controller = env.restart()
    assert controller.items == (NewsItem("info", "first"),)


def test_no_cache_and_no_network_hides_news(tmp_path):
    env = _Env(tmp_path)
    env.controller.start()
    env.loop.run()
    assert env.controller.items == ()


def test_malformed_remote_retains_valid_cache(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(3, "good"))
    env.controller.start()
    env.loop.run()
    env.remote = FetchResult("error")  # e.g. invalid JSON
    env.controller.refresh()
    env.loop.run()
    assert env.controller.items == (NewsItem("info", "good"),)


def test_dismissal_persists_across_restarts_and_offline(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(3, "news"))
    env.controller.start()
    env.loop.run()
    assert env.controller.dismiss() is True
    assert env.controller.items == ()  # hidden immediately, before persistence
    env.loop.run()
    assert nf.load_dismissed_revision(env.state_path) == 3

    controller = env.restart()  # online, same revision fetched again
    assert controller.items == ()
    env.remote = None
    controller = env.restart()  # offline, from cache
    assert controller.items == ()


def test_same_revision_never_reappears_after_refetch_or_cache_loss(tmp_path, monkeypatch):
    env = _Env(tmp_path, remote=_bulletin(3, "news"))
    env.controller.start()
    env.loop.run()
    env.controller.dismiss()
    env.loop.run()
    for _ in range(3):
        env.controller.refresh()
        env.loop.run()
        assert env.controller.items == ()
    # Cache cleared: same revision downloaded fresh is still dismissed.
    import os

    os.remove(env.cache_path)
    controller = env.restart()
    assert controller.bulletin.revision == 3
    assert controller.items == ()


def test_new_revision_restores_the_ticker(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(3, "old"))
    env.controller.start()
    env.loop.run()
    env.controller.dismiss()
    env.loop.run()
    env.remote = _bulletin(4, "new")
    env.controller.refresh()
    env.loop.run()
    assert env.controller.items == (NewsItem("info", "new"),)
    assert nf.load_dismissed_revision(env.state_path) == 3


def test_dismiss_applies_to_the_whole_bulletin(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(2, "a", "b", "c"))
    env.controller.start()
    env.loop.run()
    assert len(env.controller.items) == 3
    env.controller.dismiss()
    assert env.controller.items == ()


def test_dismissal_is_never_lowered(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(5, "x"))
    nf.save_dismissed_revision(9, env.state_path)
    env.controller.start()
    env.loop.run()
    assert env.controller.items == ()
    assert env.controller.dismiss() is False
    env.loop.run()
    assert nf.load_dismissed_revision(env.state_path) == 9


def test_hide_setting_overrides_visibility_and_skips_polling(tmp_path):
    env = _Env(tmp_path, hidden=True, remote=_bulletin(1, "news"))
    env.controller.start()
    env.loop.run()
    assert env.fetches == []  # no GitHub check while hidden
    assert env.controller.poll_active is False
    assert env.controller.items == ()
    assert env.controller.refresh() is False


def test_hiding_stops_polling_and_unhiding_fetches_without_clearing_dismissal(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(3, "news"))
    env.controller.start()
    env.loop.run()
    assert env.controller.poll_active
    env.controller.dismiss()
    env.loop.run()

    env.settings["hide_news_ticker"] = True
    env.controller.set_hidden(True)
    assert env.controller.poll_active is False and env.loop.timers == {}
    assert env.controller.items == ()

    fetches = len(env.fetches)
    env.settings["hide_news_ticker"] = False
    env.controller.set_hidden(False)
    env.loop.run()
    assert len(env.fetches) == fetches + 1  # re-enabling checks GitHub
    assert env.controller.poll_active
    assert env.controller.items == ()  # revision 3 still dismissed
    assert nf.load_dismissed_revision(env.state_path) == 3

    env.remote = _bulletin(4, "fresh")
    env.controller.refresh()
    env.loop.run()
    assert env.controller.items == (NewsItem("info", "fresh"),)


def test_unhiding_shows_an_undismissed_bulletin(tmp_path):
    env = _Env(tmp_path, hidden=True, remote=_bulletin(2, "news"))
    env.controller.start()
    env.loop.run()
    env.settings["hide_news_ticker"] = False
    env.controller.set_hidden(False)
    env.loop.run()
    assert env.controller.items == (NewsItem("info", "news"),)


def test_periodic_poll_rechecks_and_respects_hidden(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(1, "a"))
    env.controller.start()
    env.loop.run()
    (secs, tick), = env.loop.timers.values()
    assert 1800 <= secs <= 3600
    env.remote = _bulletin(2, "b")
    assert tick() is True
    env.loop.run()
    assert env.controller.items == (NewsItem("info", "b"),)
    env.settings["hide_news_ticker"] = True
    assert tick() is False


def test_single_flight_fetch(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(1, "a"))
    env.controller.start()
    assert env.controller.refresh() is False  # startup fetch still in flight
    env.loop.run()
    assert env.controller.refresh() is True


def test_shutdown_ignores_late_results_and_stops_polling(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(1, "a"))
    env.controller.start()
    env.controller.shutdown()
    env.loop.run()
    assert env.controller.items == ()
    assert env.loop.timers == {}


def test_submit_failure_is_harmless(tmp_path):
    env = _Env(tmp_path, remote=_bulletin(1, "a"))
    env.loop.submit_raises = True
    env.controller.start()
    assert env.controller.fetch_inflight is False
    assert env.controller.items == ()


def test_reset_settings_defaults_news_on_and_keeps_dismissal(tmp_path, monkeypatch):
    from dzll_launcher import settings

    monkeypatch.setattr(settings, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert settings.DEFAULTS["hide_news_ticker"] is False
    state = str(tmp_path / "news_state.json")
    nf.save_dismissed_revision(4, state)
    settings.save_settings({"hide_news_ticker": True})
    assert settings.load_settings()["hide_news_ticker"] is True
    assert settings.reset_settings()["hide_news_ticker"] is False
    assert nf.load_dismissed_revision(state) == 4
