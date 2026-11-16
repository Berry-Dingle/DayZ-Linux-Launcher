# visible_refresh_indicator.py
#
# Full-width "Updating Visible Rows" status bar beneath the server browser,
# with the text and dot at its bottom-right. The dot rests off (dim) and
# flashes on once each time the browser's visible-row live refresh actually
# applies results (see DZLLWindow._apply_browser_live_results).
# There is no periodic timer here: the only timer is the one-shot source that
# ends a blink, and it is replaced (not stacked) by a repeated refresh.

import weakref

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

VISIBLE_REFRESH_LABEL_TEXT = "Updating Visible Rows"
# Text and dot keep the former pill's right offset (14 margin + 1 border +
# 8 padding). Top 6 + 1 px border and bottom 7 centre the text vertically.
VISIBLE_REFRESH_CONTENT_MARGIN_END = 23
VISIBLE_REFRESH_CONTENT_MARGIN_BOTTOM = 7
VISIBLE_REFRESH_CONTENT_MARGIN_TOP = 6
VISIBLE_REFRESH_DOT_SIZE = 7
VISIBLE_REFRESH_DIVIDER_GAP = 4  # extra space after the divider (plus box spacing)
VISIBLE_REFRESH_PULSE_HOLD_MS = 180
VISIBLE_REFRESH_PULSE_CLASS = "visible-refresh-dot-pulse"


class VisibleRefreshIndicator:
    def __init__(self):
        self._pulse_source_id = 0
        self._closed = False

        # The bar is a normal box below the server list (not an overlay), so
        # the list's height excludes it and the last row is never covered.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.add_css_class("visible-refresh-bar")
        bar.set_hexpand(True)
        bar.set_vexpand(False)
        bar.set_can_target(False)
        bar.set_can_focus(False)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.set_hexpand(True)
        content.set_halign(Gtk.Align.END)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_end(VISIBLE_REFRESH_CONTENT_MARGIN_END)
        content.set_margin_top(VISIBLE_REFRESH_CONTENT_MARGIN_TOP)
        content.set_margin_bottom(VISIBLE_REFRESH_CONTENT_MARGIN_BOTTOM)
        bar.append(content)

        # Spans only the text row, so it never reaches the bar's borders.
        divider = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        divider.add_css_class("visible-refresh-divider")
        divider.set_valign(Gtk.Align.FILL)
        divider.set_margin_end(VISIBLE_REFRESH_DIVIDER_GAP)

        label = Gtk.Label(label=VISIBLE_REFRESH_LABEL_TEXT)
        label.add_css_class("visible-refresh-label")
        label.set_valign(Gtk.Align.CENTER)

        dot = Gtk.Box()
        dot.add_css_class("visible-refresh-dot")
        dot.set_size_request(VISIBLE_REFRESH_DOT_SIZE, VISIBLE_REFRESH_DOT_SIZE)
        dot.set_halign(Gtk.Align.CENTER)
        dot.set_valign(Gtk.Align.CENTER)

        content.append(divider)
        content.append(label)
        content.append(dot)
        # Weak reference: the widget must not keep the indicator alive, and a
        # late GTK teardown (e.g. interpreter exit) must find nothing to do.
        owner = weakref.ref(self)

        def _call(method_name):
            def _handler(*_args):
                indicator = owner()
                if indicator is not None:
                    getattr(indicator, method_name)()
            return _handler

        bar.connect("unrealize", _call("reset"))
        bar.connect("destroy", _call("cleanup"))

        self.widget = bar
        self.content = content
        self.divider = divider
        self.label = label
        self.dot = dot

    @property
    def pulse_active(self) -> bool:
        return bool(self._pulse_source_id)

    def pulse(self) -> bool:
        """Blink the dot once. A repeated call restarts the blink cleanly."""
        if self._closed:
            return False
        self._cancel_pulse_source()
        self.dot.add_css_class(VISIBLE_REFRESH_PULSE_CLASS)
        self._pulse_source_id = GLib.timeout_add(VISIBLE_REFRESH_PULSE_HOLD_MS, self._end_pulse)
        return True

    def reset(self) -> None:
        """Cancel any pending blink and return the dot to its resting state."""
        self._cancel_pulse_source()
        dot = getattr(self, "dot", None)
        if dot is not None:
            try:
                dot.remove_css_class(VISIBLE_REFRESH_PULSE_CLASS)
            except Exception:
                pass

    def cleanup(self) -> None:
        """Permanently stop the indicator (window shutdown or widget destroy)."""
        self._closed = True
        self.reset()

    def _end_pulse(self):
        self._pulse_source_id = 0
        try:
            self.dot.remove_css_class(VISIBLE_REFRESH_PULSE_CLASS)
        except Exception:
            pass
        return GLib.SOURCE_REMOVE

    def _cancel_pulse_source(self) -> None:
        source_id = getattr(self, "_pulse_source_id", 0)
        self._pulse_source_id = 0
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
