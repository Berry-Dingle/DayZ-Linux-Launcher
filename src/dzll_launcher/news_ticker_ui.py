# news_ticker_ui.py
#
# The DZLL news section at the left of the browser's bottom status bar:
#
#   [icon] DZLL NEWS | scrolling announcements ...   Dismiss | Updating Visible Rows •
#
# Announcements scroll right to left one after another, each starting with
# a bullet (warnings start with their ⚠ instead, which acts as the bullet). The next announcement enters at the right edge of the scrolling
# viewport when the trailing edge of the previous one reaches the viewport's
# midpoint, so consecutive announcements are always half a viewport apart,
# including when the bulletin loops from the last back to the first.
#
# The marquee is a single custom widget drawing one Pango layout per
# announcement inside a clip. Moving the text only changes draw positions
# (queue_draw), never the allocation, so it costs no relayout of the browser.
# The frame tick runs only while the marquee is mapped, has text and is not
# hovered.
#
# An announcement with an https URL ends with a blue, underlined "Click here".
# Only that link is clickable: hovering it shows the URL as a tooltip and a
# pointer cursor, and a click opens it in the default browser.

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gtk, Pango

from .config import IMAGES_DIR
from .ui_row import attach_pointer_cursor

NEWS_TITLE_TEXT = "DZLL NEWS"
NEWS_DISMISS_TEXT = "Dismiss"
NEWS_ICON_FILE = "newsIcon.svg"
NEWS_ICON_PX = 12
NEWS_ITEM_BULLET = "● "
# "●" at full size is too large and "•" too small; this sits between them.
NEWS_ITEM_BULLET_SCALE = 0.75
# Extra space between the bullet and the announcement text.
NEWS_ITEM_BULLET_GAP_PX = 2
NEWS_WARNING_PREFIX = "⚠ "
NEWS_ELLIPSIS_TEXT = "..."
NEWS_ELLIPSIS_GAP_PX = 4
NEWS_SCROLL_PX_PER_SEC = 32.0
# The next announcement enters when the previous one's trailing edge reaches
# this fraction of the viewport width (0.5 = the midpoint).
NEWS_NEXT_ENTRY_FRACTION = 0.5
NEWS_MAX_FRAME_STEP_SECS = 0.1
# Same vertical margins as the status text, so both rows centre identically
# and the bar keeps its height.
NEWS_SECTION_MARGIN_START = 14
NEWS_SECTION_MARGIN_TOP = 6
NEWS_SECTION_MARGIN_BOTTOM = 7
NEWS_DISMISS_MARGIN_START = 12
NEWS_DISMISS_MARGIN_END = 10

_WARNING_RGB = (0xD6, 0xA9, 0x4F)  # @dzll_warning
# A cool blue that sits beside the teal (@dzll_mod_focus #4fb8b2) accents.
_LINK_RGB = (0x5A, 0xB0, 0xE8)
NEWS_LINK_TEXT = "Click here"
NEWS_LINK_JOIN = " "


def announcement_entry(item):
    """Return (text, rgb or None, link byte range or None, url or None)."""
    text, rgb = announcement_display(item)
    url = getattr(item, "url", None)
    if not is_openable_url(url):
        return text, rgb, None, None
    start = len((text + NEWS_LINK_JOIN).encode("utf-8"))
    text = text + NEWS_LINK_JOIN + NEWS_LINK_TEXT
    return text, rgb, (start, len(text.encode("utf-8"))), url


def is_openable_url(url) -> bool:
    """Only https links (already enforced when the bulletin is parsed)."""
    return isinstance(url, str) and url.lower().startswith("https://") and not any(
        ch.isspace() for ch in url
    )


def open_news_url(url) -> bool:
    """Open a bulletin link in the default browser. Never raises."""
    if not is_openable_url(url):
        return False
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
        return True
    except Exception:
        return False


def _attr_fg(rgb, start, end):
    r, g, b = rgb
    attr = Pango.attr_foreground_new(r * 257, g * 257, b * 257)
    attr.start_index = start
    attr.end_index = end
    return attr


def announcement_display(item):
    """Return (text, rgb or None) for one bulletin item."""
    if getattr(item, "type", "info") == "warning":
        # The warning triangle is the bullet for warning items.
        return NEWS_WARNING_PREFIX + item.message, _WARNING_RGB
    return NEWS_ITEM_BULLET + item.message, None


def announcement_gap(viewport_width) -> float:
    """Space between consecutive announcements for a scrolling viewport width.

    With the next announcement entering at the right edge (x = W) when the
    previous trailing edge is at W * fraction, the gap is W * (1 - fraction).
    """
    return max(0.0, float(viewport_width)) * (1.0 - NEWS_NEXT_ENTRY_FRACTION)


def announcement_positions(widths, current, distance, viewport_width):
    """Left x of every visible announcement, as [(index, x)], newest last.

    ``current`` is the announcement that most recently entered at the right
    edge and ``distance`` how far it has travelled since its leading edge was
    at the right edge (x = viewport_width). Earlier announcements trail it by
    their width plus the gap, wrapping from the first back to the last.
    """
    count = len(widths)
    if count == 0 or viewport_width <= 0:
        return []
    gap = announcement_gap(viewport_width)
    x = float(viewport_width) - distance
    out = [(current, x)]
    index = current
    # Each step moves at least ``gap`` (> 0) left, so this terminates.
    while True:
        index = (index - 1) % count
        x -= widths[index] + gap
        if x + widths[index] <= 0:
            break
        out.append((index, x))
    out.reverse()
    return out


class NewsMarquee(Gtk.Widget):
    """Clipped right-to-left announcements, half a viewport apart, looping."""

    __gtype_name__ = "DzllNewsMarquee"

    def __init__(self):
        super().__init__()
        self.add_css_class("news-ticker-text")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        self._entries = ()  # ((text, rgb, link byte range, url), ...)
        self._link_px = []  # per entry: (x0, x1) of "Click here", or None
        self._layouts = []
        self._widths = []
        self._bullet_rise = None
        self._ellipsis_layout = None
        self._layout_font = None
        self._ellipsis_w = 0
        self._line_h = 0
        self._current = 0
        self._distance = 0.0
        self._drawn_key = None
        self._last_frame_us = None
        self._tick_id = 0
        self._paused = False
        self._closed = False
        self.connect("map", lambda *_a: self._update_ticking())
        self.connect("unmap", lambda *_a: self._update_ticking())

    # -- public --

    @property
    def text(self) -> str:
        return "\n".join(entry[0] for entry in self._entries)

    @property
    def ticking(self) -> bool:
        return bool(self._tick_id)

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def current(self) -> int:
        return self._current

    @property
    def distance(self) -> float:
        return self._distance

    @property
    def viewport_width(self) -> int:
        """Width available for scrolling text (excludes the ellipsis slot)."""
        return self._clip_width(self.get_width())

    def set_items(self, items) -> None:
        entries = tuple(announcement_entry(item) for item in items)
        if entries == self._entries:
            return
        self._entries = entries
        self._layouts = []
        self._layout_font = None
        # The first announcement starts entering at the right edge.
        self._current = 0
        self._distance = 0.0
        self._drawn_key = None
        self._last_frame_us = None
        self.queue_resize()
        self.queue_draw()
        self._update_ticking()

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        self._last_frame_us = None
        self._update_ticking()

    def cleanup(self) -> None:
        self._closed = True
        self._remove_tick()

    def item_at(self, x, width=None):
        """Index of the announcement drawn under widget x, or None."""
        self._ensure_layouts()
        viewport = self._clip_width(self.get_width() if width is None else width)
        if x < 0 or x >= viewport:
            return None
        for index, left in self.positions(width):
            if left <= x < left + self._widths[index]:
                return index
        return None

    def url_at(self, x, width=None):
        """The URL when widget x is over a "Click here" link, or None."""
        self._ensure_layouts()
        viewport = self._clip_width(self.get_width() if width is None else width)
        if x < 0 or x >= viewport:
            return None
        for index, left in self.positions(width):
            span = self._link_px[index] if index < len(self._link_px) else None
            if span is not None and left + span[0] <= x < left + span[1]:
                return self._entries[index][3]
        return None

    def positions(self, width=None):
        """[(index, x)] of the announcements visible at ``width`` (widget px)."""
        self._ensure_layouts()
        viewport = self._clip_width(self.get_width() if width is None else width)
        self._normalise(viewport)
        return announcement_positions(self._widths, self._current, self._pixel(self._distance), viewport)

    def advance(self, seconds: float, width=None) -> bool:
        """Scroll for ``seconds``. Returns True if a redraw was queued."""
        self._ensure_layouts()
        if not self._widths or seconds <= 0:
            return False
        self._distance += NEWS_SCROLL_PX_PER_SEC * seconds
        viewport = self._clip_width(self.get_width() if width is None else width)
        self._normalise(viewport)
        key = (self._current, self._pixel(self._distance))
        if key != self._drawn_key:
            self.queue_draw()
            return True
        return False

    def ellipsis_visible(self, width=None) -> bool:
        """True while the newest announcement continues past the right edge."""
        self._ensure_layouts()
        viewport = self._clip_width(self.get_width() if width is None else width)
        if not self._widths or viewport <= 0:
            return False
        self._normalise(viewport)
        return self._pixel(self._distance) < self._widths[self._current]

    # -- GTK vfuncs --

    def do_measure(self, orientation, for_size):
        self._ensure_layouts()
        if orientation == Gtk.Orientation.HORIZONTAL:
            # Takes only the space left over by its neighbours.
            return 0, 0, -1, -1
        return self._line_h, self._line_h, -1, -1

    def do_snapshot(self, snapshot):
        self._ensure_layouts()
        width = self.get_width()
        height = self.get_height()
        viewport = self._clip_width(width)
        if not self._layouts or viewport <= 0:
            return
        # A resize can shrink the gap below the current travel distance.
        self._normalise(viewport)
        color = self.get_color()
        y = max(0, (height - self._line_h) // 2)
        distance = self._pixel(self._distance)
        self._drawn_key = (self._current, distance)

        rect = Graphene.Rect()
        rect.init(0, 0, viewport, height)
        snapshot.push_clip(rect)
        for index, x in announcement_positions(self._widths, self._current, distance, viewport):
            if x >= viewport:
                continue
            snapshot.save()
            snapshot.translate(_point(x, y))
            snapshot.append_layout(self._layouts[index], color)
            snapshot.restore()
        snapshot.pop()

        if distance < self._widths[self._current]:
            snapshot.save()
            snapshot.translate(_point(viewport + NEWS_ELLIPSIS_GAP_PX, y))
            snapshot.append_layout(self._ellipsis_layout, color)
            snapshot.restore()

    # -- internals --

    def _normalise(self, viewport) -> None:
        """Hand over to the next announcement once the current one has
        travelled its width plus the gap (trailing edge at the midpoint)."""
        count = len(self._widths)
        if count == 0 or viewport <= 0:
            return
        gap = announcement_gap(viewport)
        # Bounded: each pass consumes at least ``gap`` (> 0) of distance.
        for _ in range(count * 4 + 4):
            span = self._widths[self._current] + gap
            if self._distance < span:
                return
            self._distance -= span
            self._current = (self._current + 1) % count
        self._distance = min(self._distance, self._widths[self._current] + gap)

    def _clip_width(self, width):
        return max(0, int(width) - self._ellipsis_w - NEWS_ELLIPSIS_GAP_PX)

    def _pixel(self, value) -> float:
        try:
            scale = max(1, int(self.get_scale_factor()))
        except Exception:
            scale = 1
        return round(value * scale) / scale

    def _ensure_layouts(self) -> None:
        try:
            font = self.get_pango_context().get_font_description()
            font_key = font.to_string() if font is not None else ""
        except Exception:
            font_key = ""
        if self._ellipsis_layout is not None and font_key == self._layout_font:
            return
        self._layout_font = font_key

        self._bullet_rise = None
        layouts = []
        widths = []
        line_h = 0
        link_px = []
        for text, rgb, link, _url in self._entries:
            layout = self.create_pango_layout(text)
            attrs = Pango.AttrList()
            message_end = link[0] if link else len(text.encode("utf-8"))
            if rgb is not None:
                attrs.insert(_attr_fg(rgb, 0, message_end))
            if link is not None:
                attrs.insert(_attr_fg(_LINK_RGB, link[0], link[1]))
                underline = Pango.attr_underline_new(Pango.Underline.SINGLE)
                underline.start_index, underline.end_index = link
                attrs.insert(underline)
            if text.startswith(NEWS_ITEM_BULLET):
                for attr in self._bullet_attrs():
                    attrs.insert(attr)
            layout.set_attributes(attrs)
            w, h = layout.get_pixel_size()
            if link is not None:
                # The link is the last run, so it ends at the layout width.
                x0 = layout.index_to_pos(link[0]).x / Pango.SCALE
                link_px.append((x0, float(w)))
            else:
                link_px.append(None)
            layouts.append(layout)
            widths.append(max(1, w))
            line_h = max(line_h, h)
        self._link_px = link_px
        ellipsis = self.create_pango_layout(NEWS_ELLIPSIS_TEXT)
        self._ellipsis_w, ellipsis_h = ellipsis.get_pixel_size()
        self._ellipsis_layout = ellipsis
        self._layouts = layouts
        self._widths = widths
        self._line_h = max(line_h, ellipsis_h)
        if self._widths:
            self._current %= len(self._widths)

    def _bullet_attrs(self):
        """Scale the bullet, raise it to the text's centre, and widen its space.

        Scaling keeps the glyph on the baseline, so it would sit low. The
        full-size glyph is centred on the text; raising the scaled one by the
        drop in its centre restores that. It only shrinks and moves up within
        the full glyph's extent, so the line height does not change.
        """
        bullet_end = len(NEWS_ITEM_BULLET.rstrip().encode("utf-8"))
        space_end = len(NEWS_ITEM_BULLET.encode("utf-8"))
        if self._bullet_rise is None:
            probe = self.create_pango_layout(NEWS_ITEM_BULLET.rstrip())
            ink, _logical = probe.get_extents()
            centre = probe.get_baseline() - (ink.y + ink.height / 2)
            self._bullet_rise = max(0, int(round(centre * (1.0 - NEWS_ITEM_BULLET_SCALE))))
        scale = Pango.attr_scale_new(NEWS_ITEM_BULLET_SCALE)
        rise = Pango.attr_rise_new(self._bullet_rise)
        for attr in (scale, rise):
            attr.start_index = 0
            attr.end_index = bullet_end
        # Letter spacing on the space itself lands entirely between the
        # bullet and the text (half each side of the space).
        gap = Pango.attr_letter_spacing_new(NEWS_ITEM_BULLET_GAP_PX * Pango.SCALE)
        gap.start_index = bullet_end
        gap.end_index = space_end
        return scale, rise, gap

    def _should_tick(self) -> bool:
        return (
            not self._closed
            and bool(self._entries)
            and not self._paused
            and self.get_mapped()
        )

    def _update_ticking(self) -> None:
        if self._should_tick():
            if not self._tick_id:
                self._last_frame_us = None
                self._tick_id = self.add_tick_callback(self._on_tick)
        else:
            self._remove_tick()

    def _remove_tick(self) -> None:
        tick_id, self._tick_id = self._tick_id, 0
        self._last_frame_us = None
        if tick_id:
            try:
                self.remove_tick_callback(tick_id)
            except Exception:
                pass

    def _on_tick(self, _widget, frame_clock):
        if not self._should_tick():
            self._tick_id = 0
            self._last_frame_us = None
            return GLib.SOURCE_REMOVE
        now = frame_clock.get_frame_time()
        last = self._last_frame_us
        self._last_frame_us = now
        if last is not None:
            step = min(max(0, now - last) / 1_000_000, NEWS_MAX_FRAME_STEP_SECS)
            self.advance(step)
        return GLib.SOURCE_CONTINUE


def _point(x, y):
    point = Graphene.Point()
    point.init(x, y)
    return point


class NewsTickerSection:
    """Icon, title, marquee and Dismiss, inside an always-present flexible slot.

    The slot stays visible (empty) when there is no news, so the status text
    to its right keeps its end alignment.
    """

    def __init__(self, on_dismiss, open_url=open_news_url):
        self._on_dismiss = on_dismiss
        self._open_url = open_url

        slot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        slot.add_css_class("news-ticker-slot")
        slot.set_hexpand(True)
        slot.set_valign(Gtk.Align.FILL)

        section = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        section.add_css_class("news-ticker")
        section.set_hexpand(True)
        section.set_valign(Gtk.Align.CENTER)
        section.set_margin_start(NEWS_SECTION_MARGIN_START)
        section.set_margin_top(NEWS_SECTION_MARGIN_TOP)
        section.set_margin_bottom(NEWS_SECTION_MARGIN_BOTTOM)
        section.set_visible(False)

        icon = Gtk.Image.new_from_file(os.path.join(IMAGES_DIR, NEWS_ICON_FILE))
        icon.set_pixel_size(NEWS_ICON_PX)
        icon.add_css_class("news-ticker-icon")
        icon.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(label=NEWS_TITLE_TEXT)
        title.add_css_class("news-ticker-title")
        title.set_valign(Gtk.Align.CENTER)

        divider = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        divider.add_css_class("visible-refresh-divider")
        divider.set_valign(Gtk.Align.FILL)
        divider.set_margin_end(4)

        marquee = NewsMarquee()
        hover = Gtk.EventControllerMotion.new()
        hover.connect("enter", lambda *_a: marquee.set_paused(True))
        hover.connect("leave", lambda *_a: self._on_marquee_leave())
        hover.connect("motion", lambda _c, x, _y: self._update_link_cursor(x))
        marquee.add_controller(hover)

        # Linked announcements: pointer cursor, URL tooltip, click to open.
        click = Gtk.GestureClick.new()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_marquee_released)
        marquee.add_controller(click)
        marquee.set_has_tooltip(True)
        marquee.connect("query-tooltip", self._on_marquee_query_tooltip)

        dismiss = Gtk.Button(label=NEWS_DISMISS_TEXT)
        dismiss.set_has_frame(False)
        dismiss.add_css_class("news-ticker-dismiss")
        dismiss.set_valign(Gtk.Align.CENTER)
        dismiss.set_margin_start(NEWS_DISMISS_MARGIN_START)
        dismiss.set_margin_end(NEWS_DISMISS_MARGIN_END)
        dismiss.set_tooltip_text("Hide this news bulletin")
        dismiss.connect("clicked", self._on_dismiss_clicked)
        attach_pointer_cursor(dismiss)

        for child in (icon, title, divider, marquee, dismiss):
            section.append(child)
        slot.append(section)

        self.widget = slot
        self.section = section
        self.icon = icon
        self.title = title
        self.divider = divider
        self.marquee = marquee
        self.hover = hover
        self.click = click
        self.dismiss_button = dismiss

    @property
    def shown(self) -> bool:
        return bool(self.section.get_visible())

    def set_items(self, items) -> None:
        items = tuple(items or ())
        if items:
            self.marquee.set_items(items)
            self.section.set_visible(True)
        else:
            self.section.set_visible(False)
            self.marquee.set_items(())

    def cleanup(self) -> None:
        self.marquee.cleanup()

    def _update_link_cursor(self, x) -> None:
        cursor = "pointer" if self.marquee.url_at(x) else None
        try:
            self.marquee.set_cursor(Gdk.Cursor.new_from_name(cursor) if cursor else None)
        except Exception:
            pass

    def _on_marquee_leave(self) -> None:
        self.marquee.set_paused(False)
        try:
            self.marquee.set_cursor(None)
        except Exception:
            pass

    def _on_marquee_released(self, _gesture, n_press, x, _y) -> bool:
        if n_press != 1:
            return False
        url = self.marquee.url_at(x)
        if not url:
            return False
        try:
            return bool(self._open_url(url))
        except Exception:
            return False

    def _on_marquee_query_tooltip(self, _widget, x, _y, _keyboard, tooltip) -> bool:
        url = self.marquee.url_at(x)
        if not url:
            return False
        tooltip.set_text(url)
        return True

    def _on_dismiss_clicked(self, *_args):
        # Hide at once; the controller persists the dismissal off-thread.
        self.section.set_visible(False)
        try:
            self._on_dismiss()
        except Exception:
            pass
