"""News ticker section in the browser status bar: layout, scrolling, Dismiss, Settings."""

import inspect
import time
from pathlib import Path

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from dzll_launcher import news_feed as nf
from dzll_launcher import news_ticker_ui as ntu
from dzll_launcher import visible_refresh_indicator as vri
from dzll_launcher.news_feed import NewsBulletin, NewsItem
from dzll_launcher.styles import get_app_css
from dzll_launcher.window import DZLLWindow
from test_core_startup_robustness import isolated_window_state  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
ITEMS = (
    NewsItem("info", "Welcome to the DZLL news ticker!"),
    NewsItem("warning", "This is a test warning announcement."),
    NewsItem("info", "Visit GitHub for DZLL releases.", "https://github.com/x"),
)


@pytest.fixture(autouse=True)
def gtk_display():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")


@pytest.fixture
def app_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(get_app_css("#000", 220, "#999"), -1)
    display = Gdk.Display.get_default()
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    yield
    Gtk.StyleContext.remove_provider_for_display(display, provider)


def _iterate(seconds=0.2, until=None):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if until is not None and until():
            return True
        ctx.iteration(False)
        time.sleep(0.002)
    return until() if until is not None else True


def _status_bar(width=900):
    indicator = vri.VisibleRefreshIndicator()
    ticker = ntu.NewsTickerSection(on_dismiss=lambda: None)
    indicator.set_leading(ticker.widget)
    root = Gtk.Box()
    root.add_css_class("dzll-app-root")
    root.append(indicator.widget)
    window = Gtk.Window()
    window.set_child(root)
    window.set_default_size(width, 40)
    return indicator, ticker, window


# --- layout -----------------------------------------------------------------------


def test_section_order_and_text():
    indicator, ticker, window = _status_bar()
    try:
        bar = indicator.widget
        assert bar.get_first_child() is ticker.widget
        assert ticker.widget.get_next_sibling() is indicator.content
        assert indicator.content.get_next_sibling() is None
        children = []
        child = ticker.section.get_first_child()
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        assert children == [ticker.icon, ticker.title, ticker.divider, ticker.marquee, ticker.dismiss_button]
        assert ticker.title.get_text() == "DZLL NEWS"
        assert ticker.dismiss_button.get_label() == "Dismiss"
        assert ticker.divider.get_orientation() == Gtk.Orientation.VERTICAL
        # The existing "| Updating Visible Rows" divider separates Dismiss from status.
        assert indicator.content.get_first_child() is indicator.divider
        assert ticker.widget.get_hexpand() and ticker.marquee.get_hexpand()
        assert indicator.content.get_hexpand() is False
        assert indicator.content.get_halign() == Gtk.Align.END
        # The bar must be targetable for hover/click; the status text stays inert.
        assert bar.get_can_target() is True
        assert indicator.content.get_can_target() is False
        for w in (ticker.section, ticker.icon, ticker.title, ticker.marquee, ticker.dismiss_button):
            assert w.get_valign() == Gtk.Align.CENTER
        assert ticker.section.get_margin_top() == indicator.content.get_margin_top()
        assert ticker.section.get_margin_bottom() == indicator.content.get_margin_bottom()
    finally:
        window.destroy()


def test_bar_height_is_unchanged_by_the_news_section(app_css):
    plain = vri.VisibleRefreshIndicator()
    plain_root = Gtk.Box()
    plain_root.add_css_class("dzll-app-root")
    plain_root.append(plain.widget)
    plain_h = plain.widget.measure(Gtk.Orientation.VERTICAL, -1)[:2]

    indicator, ticker, window = _status_bar()
    try:
        hidden_h = indicator.widget.measure(Gtk.Orientation.VERTICAL, -1)[:2]
        ticker.set_items(ITEMS)
        shown_h = indicator.widget.measure(Gtk.Orientation.VERTICAL, -1)[:2]
        assert plain_h == hidden_h == shown_h
        label_h = indicator.label.measure(Gtk.Orientation.VERTICAL, -1)[1]
        for w in (ticker.icon, ticker.title, ticker.marquee, ticker.dismiss_button):
            assert w.measure(Gtk.Orientation.VERTICAL, -1)[1] <= label_h
    finally:
        window.destroy()


@pytest.mark.parametrize("width", [1400, 900, 560])
def test_status_text_stays_readable_and_news_never_overlaps(app_css, width):
    indicator, ticker, window = _status_bar(width)
    try:
        ticker.set_items((NewsItem("info", "A long announcement " * 40),))
        window.present()
        assert _iterate(1.0, until=lambda: indicator.label.get_width() > 0)
        _iterate(0.1)
        label_natural = indicator.label.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        assert indicator.label.get_width() >= label_natural
        ok, bounds = ticker.dismiss_button.compute_bounds(indicator.widget)
        ok2, divider = indicator.divider.compute_bounds(indicator.widget)
        assert ok and ok2
        assert bounds.get_x() + bounds.get_width() <= divider.get_x()
        ok, marquee = ticker.marquee.compute_bounds(indicator.widget)
        assert marquee.get_x() + marquee.get_width() <= bounds.get_x()
        assert ticker.marquee.get_overflow() == Gtk.Overflow.HIDDEN
    finally:
        window.destroy()


def test_window_resize_reallocates_the_marquee(app_css):
    indicator, ticker, window = _status_bar(1300)
    try:
        # A mapped window may ignore set_default_size (Wayland), so resize the
        # bar's container instead.
        root = indicator.widget.get_parent()
        holder = Gtk.Box()
        window.set_child(holder)
        root.set_halign(Gtk.Align.START)
        holder.append(root)
        root.set_size_request(1200, -1)
        ticker.set_items(ITEMS)
        window.present()
        assert _iterate(1.0, until=lambda: ticker.marquee.get_width() > 0)
        wide = ticker.marquee.get_width()
        root.set_size_request(700, -1)
        assert _iterate(1.0, until=lambda: 0 < ticker.marquee.get_width() < wide)
        assert wide - ticker.marquee.get_width() == pytest.approx(500, abs=2)
    finally:
        window.destroy()


# --- scrolling ---------------------------------------------------------------------


def test_each_announcement_starts_with_a_bullet_and_keeps_warning_colour():
    assert ntu.announcement_display(ITEMS[0]) == ("● Welcome to the DZLL news ticker!", None)
    assert ntu.announcement_display(ITEMS[1]) == (
        "● " + ntu.NEWS_WARNING_PREFIX + "This is a test warning announcement.",
        ntu._WARNING_RGB,
    )
    marquee = ntu.NewsMarquee()
    marquee.set_items(ITEMS)
    lines = marquee.text.split("\n")
    assert lines == [ntu.announcement_display(i)[0] for i in ITEMS]
    assert all(line.startswith(ntu.NEWS_ITEM_BULLET) for line in lines)
    assert all(line.count("●") == 1 for line in lines)  # no separators between items


def test_gap_is_half_the_scrolling_viewport():
    assert ntu.announcement_gap(600) == 300
    assert ntu.announcement_gap(0) == 0


@pytest.mark.parametrize("viewport", [200, 600, 1500])
@pytest.mark.parametrize("widths", [[80, 400, 150], [900], [30, 30], [1200, 50]])
def test_next_enters_at_right_edge_when_previous_trailing_edge_hits_midpoint(viewport, widths):
    count = len(widths)
    for current in range(count):
        previous = (current - 1) % count
        # Just as `current` enters (distance 0), the previous announcement's
        # trailing edge is exactly at the midpoint; this also covers the loop
        # from the last announcement back to the first.
        pos = ntu.announcement_positions(widths, current, 0.0, viewport)
        assert pos[-1] == (current, viewport)  # newest, at the right edge
        prev_index, prev_x = pos[-2]
        assert prev_index == previous
        assert prev_x + widths[previous] == pytest.approx(viewport / 2)


@pytest.mark.parametrize("distance", [0.0, 17.5, 140.0, 200.0])
def test_separation_is_maintained_while_moving(distance):
    widths, viewport = [120, 260, 90], 500
    pos = ntu.announcement_positions(widths, 1, distance, viewport)
    xs = dict(pos)
    assert xs[1] == pytest.approx(viewport - distance)
    assert xs[1] - (xs[0] + widths[0]) == pytest.approx(viewport / 2)
    order = [i for i, _x in pos]
    for a, b in zip(order, order[1:]):
        assert xs[b] - (xs[a] + widths[a]) == pytest.approx(viewport / 2)


def test_only_visible_announcements_are_positioned():
    widths, viewport = [100, 100, 100], 400
    pos = ntu.announcement_positions(widths, 2, 50.0, viewport)
    assert all(x + widths[i] > 0 for i, x in pos)
    assert ntu.announcement_positions(widths, 0, 0.0, 0) == []
    assert ntu.announcement_positions([], 0, 0.0, 400) == []


def _viewport_marquee(items, width):
    marquee = ntu.NewsMarquee()
    marquee.set_items(items)
    marquee._ensure_layouts()
    return marquee, marquee._clip_width(width)


def test_marquee_hands_over_at_the_midpoint_and_loops():
    width = 700
    marquee, viewport = _viewport_marquee(ITEMS, width)
    widths = marquee._widths
    assert marquee.current == 0 and marquee.distance == 0.0
    assert dict(marquee.positions(width))[0] == viewport  # starts at the right edge
    marquee.advance(1.0, width=width)
    assert marquee.distance == pytest.approx(ntu.NEWS_SCROLL_PX_PER_SEC)  # constant speed

    for expected_next in (1, 2, 0):  # including the loop back to the first
        span = widths[marquee.current] + viewport / 2
        marquee.advance((span - marquee.distance - 0.5) / ntu.NEWS_SCROLL_PX_PER_SEC, width=width)
        before = marquee.current
        assert before != expected_next
        marquee.advance(1.0 / ntu.NEWS_SCROLL_PX_PER_SEC, width=width)
        assert marquee.current == expected_next
        pos = dict(marquee.positions(width))
        assert pos[expected_next] == pytest.approx(viewport - marquee.distance, abs=1)
        assert pos[before] + widths[before] == pytest.approx(viewport / 2 - marquee.distance, abs=1)


def test_single_announcement_repeats_with_the_same_spacing():
    width = 500
    marquee, viewport = _viewport_marquee((ITEMS[0],), width)
    w = marquee._widths[0]
    marquee.advance((w + viewport / 2 + 10) / ntu.NEWS_SCROLL_PX_PER_SEC, width=width)
    assert marquee.current == 0
    pos = marquee.positions(width)
    assert len(pos) == 2
    (_, old_x), (_, new_x) = pos
    assert new_x - (old_x + w) == pytest.approx(viewport / 2, abs=1)


def test_resize_keeps_the_newest_announcement_anchored_to_the_right_edge():
    marquee, viewport = _viewport_marquee(ITEMS, 1000)
    marquee.advance(3.0, width=1000)
    current, distance = marquee.current, marquee.distance
    narrow_viewport = marquee._clip_width(600)
    pos = dict(marquee.positions(600))
    assert marquee.current == current
    assert pos[current] == pytest.approx(narrow_viewport - distance, abs=1)
    # Gaps follow the new viewport width.
    xs = marquee.positions(600)
    for (a, xa), (b, xb) in zip(xs, xs[1:]):
        assert xb - (xa + marquee._widths[a]) == pytest.approx(narrow_viewport / 2, abs=1)
    # Shrinking below the current travel hands over instead of overlapping.
    marquee._distance = marquee._widths[marquee.current] + viewport / 2 - 1
    marquee.positions(200)
    assert marquee.distance < marquee._widths[marquee.current] + marquee._clip_width(200) / 2


def test_ellipsis_marks_unrevealed_text_at_the_right_edge():
    width = 300
    marquee, viewport = _viewport_marquee(ITEMS, width)
    assert marquee.ellipsis_visible(width=width) is True  # newest still entering
    w = marquee._widths[0]
    marquee.advance((w + 1) / ntu.NEWS_SCROLL_PX_PER_SEC, width=width)
    assert marquee.ellipsis_visible(width=width) is False  # fully revealed, gap at the edge
    marquee.set_items(())
    assert marquee.ellipsis_visible(width=width) is False


def test_ticking_only_while_mapped_with_text_and_not_hovered():
    indicator, ticker, window = _status_bar()
    try:
        window.present()
        _iterate(0.3)
        assert ticker.marquee.ticking is False  # no news: section hidden
        ticker.set_items(ITEMS)
        assert _iterate(1.0, until=lambda: ticker.marquee.ticking)

        ticker.hover.emit("enter", 5.0, 5.0)
        assert ticker.marquee.paused and not ticker.marquee.ticking
        paused_at = (ticker.marquee.current, ticker.marquee.distance)
        _iterate(0.3)
        assert (ticker.marquee.current, ticker.marquee.distance) == paused_at
        ticker.hover.emit("leave")
        assert ticker.marquee.ticking

        ticker.set_items(())
        assert ticker.shown is False
        assert _iterate(1.0, until=lambda: not ticker.marquee.ticking)
    finally:
        window.destroy()


def test_marquee_moves_in_a_real_frame_loop():
    indicator, ticker, window = _status_bar()
    try:
        ticker.set_items(ITEMS)
        window.present()
        assert _iterate(2.0, until=lambda: ticker.marquee.distance > 2)
    finally:
        window.destroy()


def test_marquee_uses_no_timers_and_no_relayout_per_frame():
    source = inspect.getsource(ntu)
    assert "timeout_add" not in source
    assert "add_tick_callback" in source
    advance = inspect.getsource(ntu.NewsMarquee.advance)
    assert "queue_resize" not in advance
    assert "queue_draw" in advance


def test_cleanup_stops_ticking():
    indicator, ticker, window = _status_bar()
    try:
        ticker.set_items(ITEMS)
        window.present()
        assert _iterate(1.0, until=lambda: ticker.marquee.ticking)
        ticker.cleanup()
        assert ticker.marquee.ticking is False
    finally:
        window.destroy()


# --- Dismiss ---------------------------------------------------------------------


def test_dismiss_hides_the_whole_section_immediately():
    calls = []
    ticker = ntu.NewsTickerSection(on_dismiss=lambda: calls.append(1))
    ticker.set_items(ITEMS)
    assert ticker.shown
    ticker.dismiss_button.emit("clicked")
    assert ticker.shown is False
    assert calls == [1]
    # The empty slot remains so the status text keeps its right alignment.
    assert ticker.widget.get_visible() is True


def test_empty_items_hide_the_section():
    ticker = ntu.NewsTickerSection(on_dismiss=lambda: None)
    ticker.set_items(ITEMS)
    ticker.set_items(())
    assert ticker.shown is False
    ticker.set_items(None)
    assert ticker.shown is False


def test_css_colours_are_teal_and_scoped():
    css = get_app_css("#000", 220, "#999").decode("utf-8")
    assert ".dzll-app-root .news-ticker-title," in css
    title = css[css.index(".dzll-app-root .news-ticker-title,"):]
    assert "color: @dzll_mod_focus;" in title[: title.index("}")]
    dismiss_label = css[css.index(".dzll-app-root button.flat.news-ticker-dismiss label,"):]
    assert "color: @dzll_mod_focus;" in dismiss_label[: dismiss_label.index("}")]
    icon = (ROOT / "src/dzll_launcher/images/newsIcon.svg").read_text(encoding="utf-8")
    assert 'stroke="#4FB8B2"' in icon  # @dzll_mod_focus


# --- real window integration ---------------------------------------------------------


def _write_cache(paths, revision, *messages):
    nf.save_cache(
        NewsBulletin(revision, tuple(NewsItem("info", m) for m in messages)),
        None,
        str(paths["cache"] / "news_cache.json"),
    )


def _start_news(window):
    window._news_feed.start()
    assert _iterate(3.0, until=lambda: window._news_feed._local_loaded and not window._news_feed.fetch_inflight)
    _iterate(0.05)


def test_real_window_shows_cached_news_and_dismissal_survives_restart(isolated_window_state):
    from test_core_startup_robustness import _close_window, _new_app

    paths = isolated_window_state
    _write_cache(paths, 3, "Cached hello")

    app = _new_app()
    window = DZLLWindow(app)
    try:
        bar = window._visible_refresh_indicator.widget
        assert bar.get_first_child() is window._news_ticker.widget
        assert bar.get_prev_sibling() is window.server_list_overlay
        assert window._news_ticker.shown is False  # nothing before the deferred start
        _start_news(window)  # network is stubbed to fail: cache is used
        assert window._news_ticker.shown
        assert window._news_ticker.marquee.text == "● Cached hello"

        window._news_ticker.dismiss_button.emit("clicked")
        assert window._news_ticker.shown is False
        assert _iterate(2.0, until=lambda: nf.load_dismissed_revision(str(paths["config"] / "news_state.json")) == 3)
    finally:
        _close_window(app, window)

    app = _new_app()
    window = DZLLWindow(app)
    try:
        _start_news(window)
        assert window._news_ticker.shown is False
    finally:
        _close_window(app, window)

    _write_cache(paths, 4, "Newer bulletin")
    app = _new_app()
    window = DZLLWindow(app)
    try:
        _start_news(window)
        assert window._news_ticker.shown
        assert window._news_ticker.marquee.text == "● Newer bulletin"
    finally:
        _close_window(app, window)


def test_real_window_settings_toggle_hides_and_restores(isolated_window_state, monkeypatch):
    from test_core_startup_robustness import _close_window, _new_app

    paths = isolated_window_state
    _write_cache(paths, 2, "Toggle me")
    fetches = []
    monkeypatch.setattr(
        nf, "fetch_bulletin", lambda etag=None, **_kw: fetches.append(etag) or nf.FetchResult("error")
    )

    app = _new_app()
    window = DZLLWindow(app)
    try:
        _start_news(window)
        assert window._news_ticker.shown
        switch = window._settings_widgets["hide_news_ticker"]
        assert switch.get_active() is False

        switch.set_active(True)
        assert window.settings["hide_news_ticker"] is True
        assert window._news_ticker.shown is False
        assert window._news_feed.poll_active is False

        count = len(fetches)
        switch.set_active(False)
        assert _iterate(3.0, until=lambda: len(fetches) > count and not window._news_feed.fetch_inflight)
        _iterate(0.05)
        assert window._news_ticker.shown
        assert window._news_feed.poll_active
    finally:
        _close_window(app, window)


def test_real_window_minimum_size_is_unchanged_by_news(isolated_window_state):
    from test_core_startup_robustness import _close_window, _new_app

    _write_cache(isolated_window_state, 1, "A long announcement " * 30)
    app = _new_app()
    window = DZLLWindow(app)
    try:
        child = window.get_child()
        before = (
            child.measure(Gtk.Orientation.VERTICAL, 1200)[0],
            child.measure(Gtk.Orientation.HORIZONTAL, -1)[0],
        )
        _start_news(window)
        assert window._news_ticker.shown
        after = (
            child.measure(Gtk.Orientation.VERTICAL, 1200)[0],
            child.measure(Gtk.Orientation.HORIZONTAL, -1)[0],
        )
        assert after == before
    finally:
        _close_window(app, window)


def test_shutdown_stops_news_polling_and_ticking(isolated_window_state):
    from test_core_startup_robustness import _close_window, _new_app

    _write_cache(isolated_window_state, 1, "bye")
    app = _new_app()
    window = DZLLWindow(app)
    _start_news(window)
    assert window._news_feed.poll_active
    _close_window(app, window)
    assert window._news_feed.poll_active is False
    assert window._news_ticker.marquee.ticking is False
    assert window._news_startup_source_id == 0


def test_settings_row_is_directly_below_hide_test_servers():
    source = (ROOT / "src/dzll_launcher/settings_ui.py").read_text(encoding="utf-8")
    general = source.split("    def _settings_page_general", 1)[1].split("    def _settings_page_launch", 1)[0]
    lines = [ln.strip() for ln in general.splitlines() if ln.strip()]
    hide_test = next(i for i, ln in enumerate(lines) if '"Hide Test Servers By Default"' in ln)
    assert lines[hide_test + 1] == (
        'box.append(self._settings_row_switch("Hide News Ticker", "hide_news_ticker", default=False))'
    )


def test_news_code_does_not_touch_row_tracking_or_scrolling():
    for name in (
        "_build_news_ticker",
        "_news_startup_tick",
        "_apply_news_items",
        "_on_news_dismiss_clicked",
        "_apply_news_ticker_setting",
    ):
        body = inspect.getsource(getattr(DZLLWindow, name))
        for forbidden in ("scroller", "vadjustment", "_browser_bound_rows", "_browser_live", "row_height"):
            assert forbidden not in body, (name, forbidden)
    for module in (ntu, nf):
        source = inspect.getsource(module)
        assert "BoundRowTracker" not in source and "_browser_live" not in source


def test_bullet_is_scaled_between_the_small_and_large_glyphs(app_css):
    assert 0 < ntu.NEWS_ITEM_BULLET_SCALE < 1  # shrink only: line height unchanged
    marquee = ntu.NewsMarquee()
    root = Gtk.Box()
    root.add_css_class("dzll-app-root")
    root.append(marquee)
    marquee.set_items(ITEMS)
    marquee._ensure_layouts()
    for layout in marquee._layouts:
        scales = [
            a for a in layout.get_attributes().get_attributes()
            if a.klass.type == Pango.AttrType.SCALE
        ]
        assert len(scales) == 1
        assert scales[0].start_index == 0
        assert scales[0].end_index == len("●".encode("utf-8"))
        assert scales[0].as_float().value == pytest.approx(ntu.NEWS_ITEM_BULLET_SCALE)
    label = Gtk.Label(label="Updating Visible Rows")
    label.add_css_class("visible-refresh-label")
    root.append(label)
    assert marquee.measure(Gtk.Orientation.VERTICAL, -1)[1] <= label.measure(Gtk.Orientation.VERTICAL, -1)[1]


def test_bullet_is_vertically_centred_and_followed_by_a_2px_gap(app_css):
    marquee = ntu.NewsMarquee()
    root = Gtk.Box()
    root.add_css_class("dzll-app-root")
    root.append(marquee)
    marquee.set_items((NewsItem("info", "Hello"),))
    marquee._ensure_layouts()
    layout = marquee._layouts[0]
    scale = Pango.SCALE
    bullet_end = len("●".encode("utf-8"))

    def bullet_centre(attr_list=None):
        # A bullet-only layout: its ink is the bullet's ink.
        lay = marquee.create_pango_layout("●")
        if attr_list is not None:
            lay.set_attributes(attr_list)
        ink, _logical = lay.get_extents()
        return (lay.get_baseline() - (ink.y + ink.height / 2)) / scale

    styled = Pango.AttrList()
    for attr in marquee._bullet_attrs()[:2]:  # scale and rise
        styled.insert(attr)
    text_centre = bullet_centre()  # the full-size glyph is centred on the text
    assert bullet_centre(styled) == pytest.approx(text_centre, abs=0.5)
    sunk = Pango.AttrList()
    sunk.insert(marquee._bullet_attrs()[0])
    assert bullet_centre(sunk) < text_centre - 0.5  # scaling alone sits low

    plain = marquee.create_pango_layout("● Hello")
    attrs = Pango.AttrList()
    s = Pango.attr_scale_new(ntu.NEWS_ITEM_BULLET_SCALE)
    s.start_index, s.end_index = 0, bullet_end
    attrs.insert(s)
    plain.set_attributes(attrs)
    text_start = len("● ".encode("utf-8"))
    gap = (layout.index_to_pos(text_start).x - plain.index_to_pos(text_start).x) / scale
    assert gap == pytest.approx(ntu.NEWS_ITEM_BULLET_GAP_PX, abs=0.05)
    assert layout.index_to_pos(0).x == 0  # nothing added before the bullet
