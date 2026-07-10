#!/usr/bin/env python3
# sidebar_ui.py
#
# Sidebar UI extracted from main.py with ZERO behavior change.

import os
import re
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gio, Pango, GLib, Gdk, GdkPixbuf

from .config import (
    SIDEBAR_WIDTH,
    SIDEBAR_INNER_PADDING,
    DISCLAIMER_TEXT,
    IMAGES_DIR,
)

from .settings import save_settings
from .ui_row import hr, attach_pointer_cursor
from .mod_search import parse_required_mod_query, split_mod_search_operator

_MOD_SEARCH_OPERATOR_RE = re.compile(r"(^|\s)(mods:|-mods=)", re.IGNORECASE)
_MOD_SEARCH_MODE_OPERATOR_RE = re.compile(r"(^|\s)(mods:)", re.IGNORECASE)
NORMAL_SEARCH_PLACEHOLDER = "Filter by name or IP. Click MOD for required mods."
MOD_SEARCH_PLACEHOLDER = "Search required mods. Select a known mod, or press ESC to exit."
MOD_SUGGESTION_VISIBLE_ROWS = 9
MOD_SUGGESTION_ROW_HEIGHT_ESTIMATE = 34
MOD_CHIP_VISIBLE_ROWS = 2
MOD_CHIP_NAME_READABLE_MIN_WIDTH = 104
MOD_CHIP_AREA_PAD_TOP = 0
MOD_CHIP_AREA_PAD_BOTTOM = 4
MOD_CHIP_AREA_OVERFLOW_PAD_BOTTOM = 2
MOD_CHIP_AREA_MARGIN_BOTTOM = 2
MOD_CHIP_ROW_HEIGHT_ESTIMATE = 24
MOD_CHIP_SCROLLBAR_HEIGHT_ALLOWANCE = 12


def remove_mod_chip_term_from_search_text(raw_text: str, chip_index: int) -> str:
    raw = str(raw_text or "")
    match = _MOD_SEARCH_OPERATOR_RE.search(raw)
    if not match:
        return raw

    try:
        target_index = int(chip_index)
    except Exception:
        return raw
    if target_index < 0:
        return raw

    normal_query = raw[:match.start()].strip()
    operator = match.group(2)
    raw_mod_query = raw[match.end():]
    kept_terms: list[str] = []
    current_chip_index = -1
    removed = False

    for raw_part in raw_mod_query.split(","):
        term = raw_part.strip()
        if not term:
            continue
        if not parse_required_mod_query(term):
            continue
        current_chip_index += 1
        if current_chip_index == target_index:
            removed = True
            continue
        kept_terms.append(term)

    if not removed:
        return raw
    if not kept_terms:
        return normal_query

    serialized_mods = f"{','.join(kept_terms)}, "
    if normal_query:
        return f"{normal_query} {operator}{serialized_mods}"
    return f"{operator}{serialized_mods}"


def connect_mutually_exclusive_checkbuttons(cb_a, cb_b, on_change):
    """
    When cb_a is turned ON, cb_b is forced OFF (and vice versa).
    Calls on_change() after any toggle.
    """
    guard = {"busy": False}

    def _wrap(src, other):
        def _on_toggled(_cb):
            if guard["busy"]:
                return
            try:
                guard["busy"] = True
                if src.get_active() and other.get_active():
                    other.set_active(False)
            except Exception:
                pass
            finally:
                guard["busy"] = False
            try:
                on_change()
            except Exception:
                pass
        return _on_toggled

    cb_a.connect("toggled", _wrap(cb_a, cb_b))
    cb_b.connect("toggled", _wrap(cb_b, cb_a))

def build_search_area(window) -> Gtk.Widget:
    search_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    search_frame.set_hexpand(True)
    search_frame.set_vexpand(False)

    search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    search_row.set_margin_top(8)
    search_row.set_margin_bottom(8)
    search_row.set_margin_start(10)
    search_row.set_margin_end(10)
    search_row.set_hexpand(True)
    search_frame.append(search_row)

    chip_spacing = 4
    window.mod_search_chip_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    window.mod_search_chip_area.set_margin_start(10)
    window.mod_search_chip_area.set_margin_end(10)
    window.mod_search_chip_area.set_margin_top(0)
    window.mod_search_chip_area.set_margin_bottom(MOD_CHIP_AREA_MARGIN_BOTTOM)
    window.mod_search_chip_area.set_hexpand(True)
    window.mod_search_chip_area.set_halign(Gtk.Align.FILL)
    window.mod_search_chip_area.set_visible(False)

    window.mod_search_chip_plain_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=chip_spacing)
    window.mod_search_chip_plain_row.add_css_class("mod-search-chip-row")
    window.mod_search_chip_plain_row.set_hexpand(True)
    window.mod_search_chip_plain_row.set_halign(Gtk.Align.START)
    window.mod_search_chip_plain_row.set_margin_top(MOD_CHIP_AREA_PAD_TOP)
    window.mod_search_chip_plain_row.set_margin_bottom(MOD_CHIP_AREA_PAD_BOTTOM)
    window.mod_search_chip_plain_row.set_visible(False)

    window.mod_search_chip_scroller = Gtk.ScrolledWindow()
    window.mod_search_chip_scroller.add_css_class("mod-search-chip-scroller")
    window.mod_search_chip_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
    try:
        window.mod_search_chip_scroller.set_overlay_scrolling(False)
    except Exception:
        pass
    window.mod_search_chip_scroller.set_propagate_natural_height(True)
    window.mod_search_chip_scroller.set_margin_start(0)
    window.mod_search_chip_scroller.set_margin_end(0)
    window.mod_search_chip_scroller.set_margin_top(0)
    window.mod_search_chip_scroller.set_margin_bottom(0)
    window.mod_search_chip_scroller.set_hexpand(True)
    window.mod_search_chip_scroller.set_halign(Gtk.Align.FILL)
    window.mod_search_chip_scroller.set_visible(False)

    window.mod_search_chip_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=chip_spacing)
    window.mod_search_chip_row.add_css_class("mod-search-chip-row")
    window.mod_search_chip_row.set_hexpand(True)
    window.mod_search_chip_row.set_halign(Gtk.Align.START)
    window.mod_search_chip_row.set_margin_top(MOD_CHIP_AREA_PAD_TOP)
    window.mod_search_chip_row.set_margin_bottom(MOD_CHIP_AREA_PAD_BOTTOM)
    window.mod_search_chip_row.set_visible(False)
    window.mod_search_chip_scroller.set_child(window.mod_search_chip_row)
    window._mod_search_chip_terms = ()
    window._mod_search_chip_reflow_id = 0
    window._mod_search_chip_width_watch_id = 0
    window._mod_search_chip_wrap_width = 0
    search_frame.append(window.mod_search_chip_area)

    window.mod_search_control = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    window.mod_search_control.add_css_class("mod-search-control")
    window.mod_search_control.set_hexpand(True)
    window.mod_search_control.set_halign(Gtk.Align.FILL)

    window.mod_search_toggle_badge = Gtk.ToggleButton()
    window.mod_search_toggle_badge.add_css_class("mod-search-toggle-badge")
    window.mod_search_toggle_badge.set_valign(Gtk.Align.FILL)
    window.mod_search_toggle_badge.set_halign(Gtk.Align.START)
    window.mod_search_toggle_badge.set_margin_end(4)
    window.mod_search_toggle_badge.set_can_focus(False)
    window.mod_search_toggle_badge.set_tooltip_text("Search by required mods.\nYou can also type mods:")
    attach_pointer_cursor(window.mod_search_toggle_badge)
    mod_toggle_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    mod_toggle_stack.add_css_class("mod-search-toggle-stack")
    mod_toggle_stack.set_valign(Gtk.Align.CENTER)
    mod_toggle_stack.set_halign(Gtk.Align.CENTER)
    for letter in ("M", "O", "D"):
        mod_toggle_letter = Gtk.Label(label=letter)
        mod_toggle_letter.add_css_class("mod-search-toggle-letter")
        mod_toggle_letter.set_valign(Gtk.Align.CENTER)
        mod_toggle_letter.set_halign(Gtk.Align.CENTER)
        mod_toggle_stack.append(mod_toggle_letter)
    window.mod_search_toggle_badge.set_child(mod_toggle_stack)

    window.search_entry = Gtk.Entry()
    window.search_entry.add_css_class("top-search-entry")
    window.search_entry.set_hexpand(True)
    window.search_entry.set_halign(Gtk.Align.FILL)
    window.search_entry.set_placeholder_text(NORMAL_SEARCH_PLACEHOLDER)
    window.search_entry.set_tooltip_text(
        "Filter by server name or IP. Type mods: to search required mods. Selected required mods appear as chips."
    )

    def update_search_clear_icon():
        icon = "edit-clear-symbolic" if (window.search_entry.get_text() or "") else None
        window.search_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, icon)

    def set_mod_search_mode_ui(active: bool):
        active = bool(active)
        toggle = window.mod_search_toggle_badge
        window._mod_search_toggle_update_guard = True
        try:
            toggle.set_active(active)
        finally:
            window._mod_search_toggle_update_guard = False
        if active:
            toggle.add_css_class("mod-search-toggle-badge-active")
            toggle.set_tooltip_text("Mod Search is active. Click to exit.")
        else:
            try:
                toggle.remove_css_class("mod-search-toggle-badge-active")
            except Exception:
                pass
            toggle.set_tooltip_text("Search by required mods.\nYou can also type mods:")
        if bool(active):
            window.search_entry.set_placeholder_text(MOD_SEARCH_PLACEHOLDER)
            window.search_entry.add_css_class("mod-search-entry-active")
        else:
            window.search_entry.set_placeholder_text(NORMAL_SEARCH_PLACEHOLDER)
            try:
                window.search_entry.remove_css_class("mod-search-entry-active")
            except Exception:
                pass
    window._sync_mod_search_mode_ui = set_mod_search_mode_ui

    def enter_mod_search_mode_from_toggle():
        if bool(getattr(window, "_mod_search_mode_active", False)):
            return
        saved_normal_text = window.search_entry.get_text() or ""
        window._mod_search_saved_normal_text = saved_normal_text.strip()
        window._mod_search_mode_active = True
        set_mod_search_mode_ui(True)
        window._mod_search_entry_update_guard = True
        try:
            window.search_entry.set_text("")
        finally:
            window._mod_search_entry_update_guard = False
        update_search_clear_icon()
        try:
            window._cancel_mod_suggestion_refresh()
            window._hide_mod_suggestions("mod_search_toggle")
        except Exception:
            pass
        try:
            window.search_entry.grab_focus()
            window.search_entry.set_position(-1)
        except Exception:
            pass
        window._cancel_search_filter_debounce()
        window._apply_search_filter_changed(scroll=True, reason="search")

    def on_mod_search_toggle_clicked(_button):
        if bool(getattr(window, "_mod_search_toggle_update_guard", False)):
            return
        if bool(getattr(window, "_mod_search_mode_active", False)):
            window._exit_mod_search_mode_restore()
        else:
            enter_mod_search_mode_from_toggle()

    window.mod_search_toggle_badge.connect("clicked", on_mod_search_toggle_clicked)

    def try_activate_mod_search_mode(raw_text: str) -> bool:
        if bool(getattr(window, "_mod_search_mode_active", False)):
            return False
        match = _MOD_SEARCH_MODE_OPERATOR_RE.search(str(raw_text or ""))
        if not match:
            return False

        saved_normal_text = str(raw_text or "")[:match.start()].strip()
        mod_input_text = str(raw_text or "")[match.end():].lstrip()
        complete_terms = []
        if "," in mod_input_text:
            parts = mod_input_text.split(",")
            complete_terms = [part.strip() for part in parts[:-1] if part.strip()]
            mod_input_text = parts[-1].lstrip()
        window._mod_search_saved_normal_text = saved_normal_text
        window._mod_search_mode_active = True
        set_mod_search_mode_ui(True)
        unresolved_terms = []
        for term in complete_terms:
            try:
                chip = window._resolve_exact_mod_chip(term)
                if chip is not None:
                    window._add_selected_mod_chip(chip)
                else:
                    unresolved_terms.append(term)
            except Exception:
                pass
        if unresolved_terms:
            unresolved_terms.append(mod_input_text.strip())
            mod_input_text = ", ".join(term for term in unresolved_terms if term)
        window._mod_search_entry_update_guard = True
        try:
            window.search_entry.set_text(mod_input_text)
        finally:
            window._mod_search_entry_update_guard = False
        update_search_clear_icon()
        try:
            window.search_entry.grab_focus()
            window.search_entry.set_position(-1)
        except Exception:
            pass
        try:
            GLib.idle_add(window._restore_search_entry_cursor, len(mod_input_text))
        except Exception:
            pass
        return True

    def parsed_mod_chip_terms(raw_text: str) -> tuple[str, ...]:
        _normal_query, raw_mod_query, mod_query_mode = split_mod_search_operator(raw_text)
        if not mod_query_mode:
            return ()
        return tuple(term for term, *_rest in parse_required_mod_query(raw_mod_query))

    def clear_mod_chip_row():
        for row in (window.mod_search_chip_plain_row, window.mod_search_chip_row):
            while True:
                child = row.get_first_child()
                if child is None:
                    break
                row.remove(child)
        while True:
            child = window.mod_search_chip_area.get_first_child()
            if child is None:
                break
            window.mod_search_chip_area.remove(child)

    def mod_chip_wrap_width() -> int:
        collect_debug_candidates = debug_chip_layout_enabled()
        width_candidates: list[tuple[str, int]] = []
        for label, widget in (
            ("mod_search_chip_area", getattr(window, "mod_search_chip_area", None)),
            ("mod_search_chip_plain_row", getattr(window, "mod_search_chip_plain_row", None)),
            ("mod_search_chip_scroller", getattr(window, "mod_search_chip_scroller", None)),
            ("search_row", search_row),
            ("search_entry", window.search_entry),
        ):
            if widget is None:
                continue
            try:
                width = int(widget.get_width())
            except Exception:
                width = 0
            if collect_debug_candidates:
                width_candidates.append((label, width))
            if width > 0:
                if collect_debug_candidates:
                    window._mod_search_chip_wrap_width_candidates = width_candidates
                return width
        if collect_debug_candidates:
            window._mod_search_chip_wrap_width_candidates = width_candidates
        return 0

    def make_mod_chip_line() -> Gtk.Box:
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=chip_spacing)
        line.set_hexpand(False)
        line.set_halign(Gtk.Align.START)
        return line

    def show_mod_chip_area_child(child: Gtk.Widget):
        current = window.mod_search_chip_area.get_first_child()
        if current is not child:
            if current is not None:
                window.mod_search_chip_area.remove(current)
            window.mod_search_chip_area.append(child)

    def render_mod_chip_lanes(container: Gtk.Box, packed_lanes: list[list[tuple[int, str]]]):
        for lane_items in packed_lanes:
            if not lane_items:
                continue
            line = make_mod_chip_line()
            for chip_index, term in lane_items:
                line.append(make_mod_chip(term, chip_index))
            container.append(line)

    def measured_chip_width(chip: Gtk.Widget, term: str) -> int:
        try:
            minimum, _natural, _minimum_baseline, _natural_baseline = chip.measure(Gtk.Orientation.HORIZONTAL, -1)
            if int(minimum) > 0:
                return int(minimum)
        except Exception:
            pass
        readable_name_width = min(min(max(len(str(term or "")), 1), 24) * 8, MOD_CHIP_NAME_READABLE_MIN_WIDTH)
        return readable_name_width + 34

    def measured_chip_lane_height(lane: Gtk.Widget) -> int:
        try:
            _minimum, natural, _minimum_baseline, _natural_baseline = lane.measure(Gtk.Orientation.VERTICAL, -1)
            if int(natural) > 0:
                return int(natural)
        except Exception:
            pass
        return MOD_CHIP_ROW_HEIGHT_ESTIMATE

    def set_mod_chip_scroller_height(container: Gtk.Box, overflow_exists: bool):
        visible_lanes = []
        child = container.get_first_child()
        while child is not None:
            visible_lanes.append(child)
            child = child.get_next_sibling()
        if not visible_lanes:
            window.mod_search_chip_scroller.set_size_request(-1, -1)
            return
        content_height = int(container.get_margin_top())
        content_height += sum(measured_chip_lane_height(lane) for lane in visible_lanes)
        content_height += chip_spacing * max(0, len(visible_lanes) - 1)
        content_height += int(container.get_margin_bottom())
        if overflow_exists:
            content_height += MOD_CHIP_SCROLLBAR_HEIGHT_ALLOWANCE
        window.mod_search_chip_scroller.set_size_request(-1, content_height)

    def debug_chip_layout_enabled() -> bool:
        return os.environ.get("DZLL_DEBUG_CHIP_LAYOUT") == "1"

    # Temporary chip layout dump retained for diagnosing GTK allocation issues.
    # It is silent and avoids scheduling allocation probes unless explicitly enabled.
    def debug_widget_line(label: str, widget):
        if widget is None:
            print(f"[chip-layout] widget {label}: <missing>")
            return
        try:
            widget_type = type(widget).__name__
        except Exception:
            widget_type = "<unknown>"
        try:
            parent = widget.get_parent()
            parent_type = type(parent).__name__ if parent is not None else None
        except Exception:
            parent_type = "<error>"
        try:
            allocation = widget.get_allocation()
            alloc_text = f"x={allocation.x} y={allocation.y} w={allocation.width} h={allocation.height}"
        except Exception:
            try:
                alloc_text = f"w={widget.get_width()} h={widget.get_height()}"
            except Exception:
                alloc_text = "<unavailable>"

        def call_bool(name: str) -> str:
            try:
                func = getattr(widget, name)
                return str(bool(func()))
            except Exception:
                return "?"

        def call_value(name: str) -> str:
            try:
                func = getattr(widget, name)
                return str(func())
            except Exception:
                return "?"

        try:
            size_request = widget.get_size_request()
        except Exception:
            size_request = "?"
        print(
            "[chip-layout] widget "
            f"{label}: type={widget_type} parent={parent_type} "
            f"visible={call_bool('get_visible')} mapped={call_bool('get_mapped')} "
            f"realized={call_bool('get_realized')} alloc=({alloc_text}) "
            f"margins=t{call_value('get_margin_top')} b{call_value('get_margin_bottom')} "
            f"s{call_value('get_margin_start')} e{call_value('get_margin_end')} "
            f"expand=h{call_value('get_hexpand')} v{call_value('get_vexpand')} "
            f"align=h{call_value('get_halign')} v{call_value('get_valign')} "
            f"size_request={size_request}"
        )

    def debug_dump_chip_layout(reason: str):
        if not debug_chip_layout_enabled():
            return False
        state = getattr(window, "_mod_search_chip_debug_state", {}) or {}
        print(f"[chip-layout] === dump reason={reason} ===")
        print(
            "[chip-layout] state "
            f"selected_chip_count={state.get('selected_chip_count')} "
            f"visible_lane_count={state.get('visible_lane_count')} "
            f"lane_item_counts={state.get('lane_item_counts')} "
            f"lane_packed_widths={state.get('lane_packed_widths')} "
            f"wrap_width={state.get('wrap_width')} "
            f"wrap_width_candidates={state.get('wrap_width_candidates')} "
            f"wrap_width_subtractions={state.get('wrap_width_subtractions')} "
            f"overflow_exists={state.get('overflow_exists')} "
            f"overflow_mode={state.get('overflow_mode')} "
            f"render_mode={state.get('render_mode')} "
            f"hscroll_policy={state.get('hscroll_policy')} "
            f"scroller_size_request={state.get('scroller_size_request')}"
        )
        print(
            "[chip-layout] constants "
            f"pad_top={MOD_CHIP_AREA_PAD_TOP} pad_bottom={MOD_CHIP_AREA_PAD_BOTTOM} "
            f"overflow_pad_bottom={MOD_CHIP_AREA_OVERFLOW_PAD_BOTTOM} "
            f"area_margin_bottom={MOD_CHIP_AREA_MARGIN_BOTTOM} "
            f"row_spacing={chip_spacing} "
            f"scrollbar_allowance={MOD_CHIP_SCROLLBAR_HEIGHT_ALLOWANCE} "
            f"readable_min_width={MOD_CHIP_NAME_READABLE_MIN_WIDTH}"
        )

        active_child = None
        try:
            active_child = window.mod_search_chip_area.get_first_child()
        except Exception:
            pass
        widgets = [
            ("search_frame", search_frame),
            ("search_row", search_row),
            ("mod_search_control", getattr(window, "mod_search_control", None)),
            ("mod_search_chip_area", getattr(window, "mod_search_chip_area", None)),
            ("mod_search_chip_plain_row", getattr(window, "mod_search_chip_plain_row", None)),
            ("mod_search_chip_scroller", getattr(window, "mod_search_chip_scroller", None)),
            ("mod_search_chip_row", getattr(window, "mod_search_chip_row", None)),
            ("active_chip_area_child", active_child),
            ("search_frame_parent", search_frame.get_parent() if search_frame is not None else None),
            ("main_browser_box", getattr(window, "main_browser_box", None)),
            ("list_view", getattr(window, "list_view", None)),
        ]
        for label, widget in widgets:
            debug_widget_line(label, widget)

        active_lane_container = None
        if state.get("render_mode") == "scroller":
            active_lane_container = getattr(window, "mod_search_chip_row", None)
        elif state.get("render_mode") == "plain":
            active_lane_container = getattr(window, "mod_search_chip_plain_row", None)
        if active_lane_container is not None:
            lane_index = 0
            lane = active_lane_container.get_first_child()
            while lane is not None:
                debug_widget_line(f"lane[{lane_index}]", lane)
                first_chip = lane.get_first_child()
                if first_chip is not None:
                    debug_widget_line(f"lane[{lane_index}].first_chip", first_chip)
                lane = lane.get_next_sibling()
                lane_index += 1
        print("[chip-layout] === end dump ===")
        return False

    def schedule_debug_chip_layout_dump(reason: str):
        if not debug_chip_layout_enabled():
            return
        GLib.timeout_add(50, debug_dump_chip_layout, reason)

    def readable_chip_name_min_width(label: Gtk.Label, term: str) -> int:
        try:
            _minimum, natural, _minimum_baseline, _natural_baseline = label.measure(Gtk.Orientation.HORIZONTAL, -1)
            natural_width = int(natural)
        except Exception:
            natural_width = 0
        if natural_width <= 0:
            natural_width = min(max(len(str(term or "")), 1), 24) * 8
        return min(natural_width, MOD_CHIP_NAME_READABLE_MIN_WIDTH)

    def remove_mod_chip(chip_index: int):
        if getattr(window, "_selected_mod_chips", None):
            try:
                window._remove_selected_mod_chip(chip_index)
            except Exception:
                pass
            return
        entry = window.search_entry
        new_text = remove_mod_chip_term_from_search_text(entry.get_text() or "", chip_index)
        if new_text == (entry.get_text() or ""):
            return
        entry.set_text(new_text)
        try:
            entry.set_position(-1)
        except Exception:
            pass

    def make_mod_chip(term: str, chip_index: int) -> Gtk.Widget:
        chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        chip.add_css_class("mod-search-chip")
        chip.set_can_focus(False)
        chip.set_hexpand(False)
        chip.set_halign(Gtk.Align.START)

        label = Gtk.Label(label=term)
        label.add_css_class("mod-search-chip-name")
        label.set_hexpand(False)
        label.set_halign(Gtk.Align.START)
        label.set_single_line_mode(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(24)
        label.set_tooltip_text(term)
        label.set_size_request(readable_chip_name_min_width(label, term), -1)
        chip.append(label)

        close_segment = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        close_segment.add_css_class("mod-search-chip-close")
        close_segment.set_can_focus(False)
        close_segment.set_hexpand(False)
        close_segment.set_halign(Gtk.Align.START)
        close_segment.set_valign(Gtk.Align.FILL)
        close_segment.set_tooltip_text(f"Remove {term}")
        attach_pointer_cursor(close_segment)

        close_click = Gtk.GestureClick.new()
        close_click.set_button(0)
        close_click.connect("released", lambda *_: remove_mod_chip(chip_index))
        close_segment.add_controller(close_click)

        close_preview = Gtk.Label(label="×")
        close_preview.add_css_class("mod-search-chip-x")
        close_preview.set_can_focus(False)
        close_preview.set_hexpand(False)
        close_preview.set_halign(Gtk.Align.CENTER)
        close_preview.set_valign(Gtk.Align.CENTER)
        close_preview.set_tooltip_text(f"Remove {term}")
        close_segment.append(close_preview)
        chip.append(close_segment)
        return chip

    def rebuild_mod_chip_row(terms: tuple[str, ...]):
        clear_mod_chip_row()
        if not terms:
            window.mod_search_chip_area.set_visible(False)
            window.mod_search_chip_plain_row.set_visible(False)
            window.mod_search_chip_row.set_visible(False)
            window.mod_search_chip_scroller.set_visible(False)
            window.mod_search_chip_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
            window.mod_search_chip_scroller.set_margin_bottom(0)
            window.mod_search_chip_scroller.set_size_request(-1, -1)
            window._mod_search_chip_wrap_width = 0
            stop_mod_chip_width_watch()
            if debug_chip_layout_enabled():
                window._mod_search_chip_debug_state = {
                    "selected_chip_count": 0,
                    "visible_lane_count": 0,
                    "lane_item_counts": [],
                    "lane_packed_widths": [],
                    "wrap_width": 0,
                    "wrap_width_candidates": getattr(window, "_mod_search_chip_wrap_width_candidates", []),
                    "wrap_width_subtractions": "none",
                    "overflow_exists": False,
                    "overflow_mode": False,
                    "render_mode": "hidden",
                    "hscroll_policy": "NEVER",
                    "scroller_size_request": window.mod_search_chip_scroller.get_size_request(),
                }
                schedule_debug_chip_layout_dump("rebuild-empty")
            return

        wrap_width = mod_chip_wrap_width()
        window._mod_search_chip_wrap_width = wrap_width
        packed_lanes: list[list[tuple[int, str]]] = [[]]
        lane_widths = [0]
        lane_counts = [0]
        active_lane_index = 0
        overflow_mode = False

        for chip_index, term in enumerate(terms):
            chip = make_mod_chip(term, chip_index)
            chip_width = measured_chip_width(chip, term)
            if overflow_mode:
                active_lane_index = min(range(len(lane_widths)), key=lambda idx: lane_widths[idx])
            current_width = lane_widths[active_lane_index]
            current_count = lane_counts[active_lane_index]
            next_width = chip_width if current_count == 0 else current_width + chip_spacing + chip_width
            if (
                not overflow_mode
                and wrap_width > 0
                and current_count > 0
                and next_width > wrap_width
                and active_lane_index < MOD_CHIP_VISIBLE_ROWS - 1
            ):
                active_lane_index += 1
                if active_lane_index >= len(packed_lanes):
                    packed_lanes.append([])
                    lane_widths.append(0)
                    lane_counts.append(0)
                current_width = lane_widths[active_lane_index]
                current_count = lane_counts[active_lane_index]
                next_width = chip_width if current_count == 0 else current_width + chip_spacing + chip_width
            elif (
                not overflow_mode
                and wrap_width > 0
                and current_count > 0
                and next_width > wrap_width
                and active_lane_index >= MOD_CHIP_VISIBLE_ROWS - 1
            ):
                overflow_mode = True
                active_lane_index = min(range(len(lane_widths)), key=lambda idx: lane_widths[idx])
                current_width = lane_widths[active_lane_index]
                current_count = lane_counts[active_lane_index]
                next_width = chip_width if current_count == 0 else current_width + chip_spacing + chip_width

            packed_lanes[active_lane_index].append((chip_index, term))
            lane_widths[active_lane_index] = next_width
            lane_counts[active_lane_index] = current_count + 1

        overflow_exists = bool(wrap_width > 0 and any(width > wrap_width for width in lane_widths))
        render_mode = "scroller" if overflow_exists else "plain"
        if overflow_exists:
            window.mod_search_chip_row.set_margin_top(MOD_CHIP_AREA_PAD_TOP)
            window.mod_search_chip_row.set_margin_bottom(MOD_CHIP_AREA_OVERFLOW_PAD_BOTTOM)
            render_mod_chip_lanes(window.mod_search_chip_row, packed_lanes)
            window.mod_search_chip_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            window.mod_search_chip_scroller.set_margin_bottom(0)
            set_mod_chip_scroller_height(window.mod_search_chip_row, True)
            show_mod_chip_area_child(window.mod_search_chip_scroller)
            window.mod_search_chip_plain_row.set_visible(False)
            window.mod_search_chip_row.set_visible(True)
            window.mod_search_chip_scroller.set_visible(True)
        else:
            window.mod_search_chip_plain_row.set_margin_top(MOD_CHIP_AREA_PAD_TOP)
            window.mod_search_chip_plain_row.set_margin_bottom(MOD_CHIP_AREA_PAD_BOTTOM)
            render_mod_chip_lanes(window.mod_search_chip_plain_row, packed_lanes)
            window.mod_search_chip_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
            window.mod_search_chip_scroller.set_size_request(-1, -1)
            window.mod_search_chip_scroller.set_visible(False)
            window.mod_search_chip_row.set_visible(False)
            show_mod_chip_area_child(window.mod_search_chip_plain_row)
            window.mod_search_chip_plain_row.set_visible(True)
        window.mod_search_chip_area.set_visible(True)
        if debug_chip_layout_enabled():
            try:
                hscroll_policy = window.mod_search_chip_scroller.get_policy()[0]
            except Exception:
                hscroll_policy = "?"
            window._mod_search_chip_debug_state = {
                "selected_chip_count": len(terms),
                "visible_lane_count": sum(1 for count in lane_counts if count > 0),
                "lane_item_counts": list(lane_counts),
                "lane_packed_widths": list(lane_widths),
                "wrap_width": wrap_width,
                "wrap_width_candidates": getattr(window, "_mod_search_chip_wrap_width_candidates", []),
                "wrap_width_subtractions": "none",
                "overflow_exists": overflow_exists,
                "overflow_mode": overflow_mode,
                "render_mode": render_mode,
                "hscroll_policy": hscroll_policy,
                "scroller_size_request": window.mod_search_chip_scroller.get_size_request(),
            }
            schedule_debug_chip_layout_dump(f"rebuild-{render_mode}")

    def schedule_mod_chip_reflow():
        if int(getattr(window, "_mod_search_chip_reflow_id", 0) or 0):
            return

        def reflow_once():
            window._mod_search_chip_reflow_id = 0
            terms = getattr(window, "_mod_search_chip_terms", ())
            if terms:
                rebuild_mod_chip_row(terms)
            return False

        window._mod_search_chip_reflow_id = GLib.idle_add(reflow_once)

    def stop_mod_chip_width_watch():
        watch_id = int(getattr(window, "_mod_search_chip_width_watch_id", 0) or 0)
        if not watch_id:
            return
        try:
            GLib.source_remove(watch_id)
        except Exception:
            pass
        window._mod_search_chip_width_watch_id = 0

    def start_mod_chip_width_watch():
        if int(getattr(window, "_mod_search_chip_width_watch_id", 0) or 0):
            return

        def width_watch_tick():
            terms = getattr(window, "_mod_search_chip_terms", ())
            if not terms:
                window._mod_search_chip_width_watch_id = 0
                return False
            width = mod_chip_wrap_width()
            if width > 0 and width != int(getattr(window, "_mod_search_chip_wrap_width", 0) or 0):
                schedule_mod_chip_reflow()
            return True

        window._mod_search_chip_width_watch_id = GLib.timeout_add(150, width_watch_tick)

    def refresh_mod_search_chips():
        selected_chips = [chip for chip in (getattr(window, "_selected_mod_chips", []) or []) if isinstance(chip, dict)]
        if selected_chips:
            terms = tuple(str(chip.get("display_name") or chip.get("query_term") or "").strip() for chip in selected_chips)
            terms = tuple(term for term in terms if term)
            if terms == getattr(window, "_mod_search_chip_terms", ()):
                start_mod_chip_width_watch()
                return
            window._mod_search_chip_terms = terms
            rebuild_mod_chip_row(terms)
            start_mod_chip_width_watch()
            schedule_mod_chip_reflow()
            return

        if bool(getattr(window, "_mod_search_mode_active", False)):
            terms = ()
            if terms == getattr(window, "_mod_search_chip_terms", ()):
                stop_mod_chip_width_watch()
                return
            window._mod_search_chip_terms = terms
            rebuild_mod_chip_row(terms)
            stop_mod_chip_width_watch()
            return
        terms = parsed_mod_chip_terms(window.search_entry.get_text() or "")
        if terms == getattr(window, "_mod_search_chip_terms", ()):
            if terms:
                start_mod_chip_width_watch()
            else:
                stop_mod_chip_width_watch()
            return
        window._mod_search_chip_terms = terms
        rebuild_mod_chip_row(terms)
        if terms:
            start_mod_chip_width_watch()
            schedule_mod_chip_reflow()
        else:
            stop_mod_chip_width_watch()

    def on_chip_wrap_width_changed(*_):
        terms = getattr(window, "_mod_search_chip_terms", ())
        if not terms:
            return
        width = mod_chip_wrap_width()
        if width > 0 and width != int(getattr(window, "_mod_search_chip_wrap_width", 0) or 0):
            schedule_mod_chip_reflow()

    for widget in (
        search_frame,
        search_row,
        window.search_entry,
        window.mod_search_chip_area,
        window.mod_search_chip_plain_row,
        window.mod_search_chip_row,
        window.mod_search_chip_scroller,
    ):
        try:
            widget.connect("notify::width", on_chip_wrap_width_changed)
        except Exception:
            pass

    def on_search_changed(*_):
        if bool(getattr(window, "_mod_search_entry_update_guard", False)):
            return
        activated_mod_search = try_activate_mod_search_mode(window.search_entry.get_text() or "")
        update_search_clear_icon()
        refresh_mod_search_chips()
        try:
            if activated_mod_search:
                window._cancel_mod_suggestion_refresh()
                window._hide_mod_suggestions("mod_search_activation")
            elif bool(getattr(window, "_mod_search_mode_active", False)):
                window._cancel_mod_suggestion_refresh()
                window._mod_suggestion_dismissed = None
                window._refresh_mod_suggestions()
            else:
                window._queue_mod_suggestions_refresh()
        except Exception:
            pass
        window._on_search_changed_debounced()

    def on_search_icon_press(_entry, position):
        if position == Gtk.EntryIconPosition.SECONDARY:
            window.search_entry.set_text("")
            try:
                window._cancel_mod_suggestion_refresh()
                window._hide_mod_suggestions("clear_icon")
            except Exception:
                pass
            window._cancel_search_filter_debounce()
            window._apply_search_filter_changed(scroll=True, reason="search")

    def on_search_cursor_position_changed(*_):
        try:
            window._cancel_mod_suggestion_refresh()
            window._refresh_mod_suggestions()
        except Exception:
            pass

    update_search_clear_icon()
    window._refresh_mod_search_chips = refresh_mod_search_chips
    window.search_entry.connect("changed", on_search_changed)
    window.search_entry.connect("notify::cursor-position", on_search_cursor_position_changed)
    window.search_entry.connect("icon-press", on_search_icon_press)
    search_key = Gtk.EventControllerKey.new()
    search_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    def on_search_key_pressed(controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape and bool(getattr(window, "_mod_search_mode_active", False)):
            return window._exit_mod_search_mode_restore()
        return window._on_mod_suggestion_key_pressed(controller, keyval, keycode, state)

    search_key.connect("key-pressed", on_search_key_pressed)
    window.search_entry.add_controller(search_key)
    search_focus = Gtk.EventControllerFocus.new()
    search_focus.connect("leave", lambda *_: GLib.idle_add(window._hide_mod_suggestions, "focus_lost"))
    window.search_entry.add_controller(search_focus)
    search_row.append(window.mod_search_toggle_badge)
    window.mod_search_control.append(window.search_entry)
    search_row.append(window.mod_search_control)

    window.mod_suggestion_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    window.mod_suggestion_panel.add_css_class("mod-suggestion-panel")
    window.mod_suggestion_panel.set_visible(False)
    window.mod_suggestion_panel.set_can_focus(False)
    window.mod_suggestion_panel.set_hexpand(True)
    window.mod_suggestion_panel.set_halign(Gtk.Align.FILL)
    window.mod_suggestion_panel.set_valign(Gtk.Align.START)
    window.mod_suggestion_panel.set_margin_top(43)
    suggestion_margin_start = int(getattr(window, "_search_area_overlay_margin_start", 0) or 0) + 10
    window.mod_suggestion_panel.set_margin_start(suggestion_margin_start)
    window.mod_suggestion_panel.set_margin_end(10)

    window.mod_suggestion_scroller = Gtk.ScrolledWindow()
    window.mod_suggestion_scroller.add_css_class("mod-suggestion-scroller")
    window.mod_suggestion_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    try:
        window.mod_suggestion_scroller.set_overlay_scrolling(False)
    except Exception:
        pass
    window.mod_suggestion_scroller.set_propagate_natural_height(True)
    window.mod_suggestion_scroller.set_max_content_height(
        MOD_SUGGESTION_VISIBLE_ROWS * MOD_SUGGESTION_ROW_HEIGHT_ESTIMATE
    )
    window.mod_suggestion_scroller.set_hexpand(True)
    window.mod_suggestion_scroller.set_halign(Gtk.Align.FILL)

    window.mod_suggestion_list = Gtk.ListBox()
    window.mod_suggestion_list.add_css_class("mod-suggestion-list")
    window.mod_suggestion_list.set_can_focus(False)
    window.mod_suggestion_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
    window.mod_suggestion_list.set_activate_on_single_click(True)
    window.mod_suggestion_list.connect("row-activated", window._on_mod_suggestion_row_activated)
    window.mod_suggestion_scroller.set_child(window.mod_suggestion_list)
    window.mod_suggestion_panel.append(window.mod_suggestion_scroller)
    suggestion_overlay = getattr(window, "_search_area_overlay", None) or window._main_overlay
    suggestion_overlay.add_overlay(window.mod_suggestion_panel)

    return search_frame

def build_sidebar_toolbar(window) -> Gtk.Widget:
    toolbar_cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    toolbar_cell.set_size_request(SIDEBAR_WIDTH, -1)
    toolbar_cell.set_hexpand(False)
    toolbar_cell.set_halign(Gtk.Align.START)
    toolbar_cell.set_valign(Gtk.Align.START)

    search_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    search_header.set_margin_top(10)
    search_header.set_margin_bottom(8)
    search_header.set_margin_start(SIDEBAR_INNER_PADDING)
    search_header.set_margin_end(SIDEBAR_INNER_PADDING)
    search_header.set_hexpand(True)
    search_header.set_halign(Gtk.Align.START)
    search_header.set_valign(Gtk.Align.CENTER)

    settings_btn = Gtk.Button()
    settings_btn.set_can_focus(False)
    settings_btn.add_css_class("flat")
    settings_btn.set_child(Gtk.Image.new_from_icon_name("preferences-system-symbolic"))
    settings_btn.set_tooltip_text("Settings")
    try:
        settings_btn.set_accessible_name("Settings")
    except Exception:
        pass
    settings_btn.connect("clicked", window._on_settings_clicked)
    attach_pointer_cursor(settings_btn)
    search_header.append(settings_btn)

    window.refresh_status_btn = Gtk.Button()
    window.refresh_status_btn.set_can_focus(False)
    window.refresh_status_btn.add_css_class("flat")
    window.refresh_status_btn.set_child(Gtk.Image.new_from_icon_name("view-refresh-symbolic"))
    window.refresh_status_btn.set_tooltip_text("Refresh live ping, player count, queue and online status for all servers.")
    window.refresh_status_btn.connect("clicked", window._on_refresh_status_clicked)
    attach_pointer_cursor(window.refresh_status_btn)
    search_header.append(window.refresh_status_btn)

    toolbar_cell.append(search_header)
    return toolbar_cell

def build_sidebar(window, include_toolbar: bool = True) -> Gtk.Widget:
    """
    Builds the entire sidebar and assigns the same widget refs onto `window`:
      - window.map_model
      - window.map_dropdown
      - window.cb_show_fav / cb_1pp_only / cb_no_password / cb_online_only / cb_played_only
      - window.reset_btn
    """
    sidebar_frame = Gtk.Overlay()
    sidebar_frame.add_css_class("sidebar-frame")
    sidebar_frame.set_size_request(SIDEBAR_WIDTH, -1)
    sidebar_frame.set_hexpand(False)
    sidebar_frame.set_vexpand(True)
    sidebar_frame.set_halign(Gtk.Align.START)

    sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    sidebar.set_margin_top(max(0, SIDEBAR_INNER_PADDING - 2))
    sidebar.set_margin_bottom(SIDEBAR_INNER_PADDING)
    sidebar.set_margin_start(SIDEBAR_INNER_PADDING)
    sidebar.set_margin_end(SIDEBAR_INNER_PADDING)
    sidebar.set_hexpand(True)
    sidebar.set_halign(Gtk.Align.FILL)
    sidebar.set_vexpand(True)
    sidebar_frame.set_child(sidebar)

    if include_toolbar:
        sidebar.append(build_sidebar_toolbar(window))

    def timed_filter_callback(action: str, reason: str):
        suppressed = False
        try:
            suppressed = bool(window._filter_refresh_is_suppressed())
        except Exception:
            suppressed = False
        start = time.perf_counter() if getattr(window, "_filter_timing_enabled", False) and not suppressed else None
        ctx = None
        if start is not None:
            make_ctx = getattr(window, "_filter_timing_context", None)
            if callable(make_ctx):
                ctx = make_ctx(action, start)
                if isinstance(ctx, dict):
                    ctx["callback_start"] = start
        window._on_filter_changed(scroll=True, reason=reason, skip_gtk_filter=True, timing_ctx=ctx)

    def timed_perspective_callback(*_args):
        if bool(window.cb_1pp_only.get_active()):
            action = "1pp-only"
        elif bool(window.cb_3pp_only.get_active()):
            action = "3pp-only"
        else:
            action = "perspective-cleared"
        timed_filter_callback(action, "perspective")

    def sidebar_mini_toggle_row(label, active=False, tooltip=None):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_hexpand(True)
        row.set_halign(Gtk.Align.FILL)

        toggle = Gtk.ToggleButton()
        toggle.add_css_class("sidebar-mini-toggle")
        toggle.set_hexpand(False)
        toggle.set_halign(Gtk.Align.START)
        toggle.set_valign(Gtk.Align.CENTER)
        toggle.set_size_request(40, 20)

        track = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        track.add_css_class("sidebar-mini-switch")
        track.set_size_request(40, 20)
        knob = Gtk.Box()
        knob.add_css_class("sidebar-mini-switch-knob")
        knob.set_size_request(14, 14)
        track.append(knob)
        toggle.set_child(track)

        def sync_visual_state(_toggle=None):
            if bool(toggle.get_active()):
                track.add_css_class("sidebar-mini-switch-on")
                knob.set_margin_start(20)
            else:
                try:
                    track.remove_css_class("sidebar-mini-switch-on")
                except Exception:
                    pass
                knob.set_margin_start(0)

        toggle.connect("toggled", sync_visual_state)
        toggle.set_active(bool(active))
        sync_visual_state()
        attach_pointer_cursor(toggle)

        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_hexpand(True)
        lbl.set_halign(Gtk.Align.FILL)

        if tooltip:
            row.set_tooltip_text(tooltip)
            toggle.set_tooltip_text(tooltip)
            lbl.set_tooltip_text(tooltip)

        row.append(toggle)
        row.append(lbl)
        return row, toggle

    window.map_model = Gtk.StringList.new(["All Maps"])
    window.map_dropdown = Gtk.DropDown.new(window.map_model, None)
    window.map_dropdown.connect("notify::selected", lambda *_: timed_filter_callback("map", "map"))
    sidebar.append(window.map_dropdown)

    row, window.cb_show_fav = sidebar_mini_toggle_row("Show Favourites")
    window.cb_show_fav.connect("toggled", lambda *_: timed_filter_callback("show-favorites", "favourites"))
    sidebar.append(row)

    row, window.cb_1pp_only = sidebar_mini_toggle_row("1st Person Only")
    sidebar.append(row)

    row, window.cb_3pp_only = sidebar_mini_toggle_row("3rd Person Only")
    sidebar.append(row)

    connect_mutually_exclusive_checkbuttons(
        window.cb_1pp_only,
        window.cb_3pp_only,
        timed_perspective_callback,
    )

    row, window.cb_no_password = sidebar_mini_toggle_row("No Password")
    window.cb_no_password.connect("toggled", lambda *_: timed_filter_callback("no-password", "password"))
    sidebar.append(row)

    row, window.cb_online_only = sidebar_mini_toggle_row("Online Only")
    window.cb_online_only.connect("toggled", lambda *_: timed_filter_callback("online-only", "online"))
    sidebar.append(row)

    row, window.cb_played_only = sidebar_mini_toggle_row("Previously Joined", tooltip="Show servers you have joined before")
    window.cb_played_only.connect("toggled", lambda *_: timed_filter_callback("played-only", "played"))
    sidebar.append(row)

    sidebar.append(hr())

    window.reset_btn = Gtk.Button(label="RESET")
    window.reset_btn.connect("clicked", window._on_reset_clicked)
    sidebar.append(window.reset_btn)

    sidebar.append(hr())

    if not hasattr(window, "_sidebar_settings_widgets"):
        window._sidebar_settings_widgets = {}

    if not getattr(window, "_sidebar_compact_entry_css_loaded", False):
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            entry.sidebar-compact-entry {
                padding-top: 2px;
                padding-bottom: 2px;
                padding-left: 6px;
                padding-right: 8px;
                min-height: 0;
            }

            button.sidebar-mini-toggle,
            button.sidebar-mini-toggle:hover,
            button.sidebar-mini-toggle:active,
            button.sidebar-mini-toggle:checked {
                background: transparent;
                border: 0;
                box-shadow: none;
                outline: none;
                padding: 0;
                min-height: 0;
                min-width: 0;
            }

            box.sidebar-mini-switch {
                background: #2f3438;
                border: 1px solid alpha(@theme_text_color, 0.22);
                border-radius: 999px;
                padding: 2px;
                min-height: 0;
                min-width: 0;
            }

            box.sidebar-mini-switch:hover {
                background: #3a4045;
            }

            box.sidebar-mini-switch.sidebar-mini-switch-on {
                background: #0d686c;
                border-color: #79aeb0;
            }

            box.sidebar-mini-switch.sidebar-mini-switch-on:hover {
                background: #118084;
            }

            box.sidebar-mini-switch-knob {
                background: alpha(@theme_text_color, 0.72);
                border-radius: 999px;
                min-height: 0;
                min-width: 0;
            }

            box.sidebar-mini-switch.sidebar-mini-switch-on box.sidebar-mini-switch-knob {
                background: #ffffff;
            }
            """
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            window._sidebar_compact_entry_css_provider = provider
            window._sidebar_compact_entry_css_loaded = True

    def entry_has_focus(entry):
        try:
            return bool(entry.has_focus())
        except Exception:
            return False

    def set_entry_text_if_changed(entry, text):
        text = "" if text is None else str(text)
        if (entry.get_text() or "") == text:
            return
        try:
            pos = int(entry.get_position())
        except Exception:
            pos = -1
        entry.set_text(text)
        if pos >= 0:
            try:
                entry.set_position(min(pos, len(text)))
            except Exception:
                pass

    def sync_settings_widget(key):
        widget = getattr(window, "_settings_widgets", {}).get(key)
        if widget is None:
            return
        old_guard = bool(getattr(window, "_settings_update_guard", False))
        try:
            window._settings_update_guard = True
            val = window.settings.get(key)
            if isinstance(widget, Gtk.Entry):
                if not entry_has_focus(widget):
                    set_entry_text_if_changed(widget, "" if val is None else str(val))
            elif isinstance(widget, (Gtk.Switch, Gtk.CheckButton, Gtk.ToggleButton)):
                if bool(widget.get_active()) != bool(val):
                    widget.set_active(bool(val))
        finally:
            window._settings_update_guard = old_guard

    def apply_sidebar_setting(key):
        ctx = None
        if getattr(window, "_filter_timing_enabled", False):
            make_ctx = getattr(window, "_filter_timing_context", None)
            if callable(make_ctx):
                ctx = make_ctx(f"sidebar-setting:{key}", time.perf_counter())
                if isinstance(ctx, dict):
                    ctx["defer_log"] = True
        save_start = time.perf_counter() if ctx is not None else None
        try:
            save_settings(window.settings)
        except Exception:
            pass
        if ctx is not None:
            ctx["settings_save_ms"] = (time.perf_counter() - save_start) * 1000.0
        sync_settings_widget(key)
        runtime_start = time.perf_counter() if ctx is not None else None
        old_timing_ctx = getattr(window, "_filter_timing_current_ctx", None)
        try:
            if ctx is not None:
                window._filter_timing_current_ctx = ctx
            window._apply_setting_runtime_effects(key)
        except Exception:
            pass
        finally:
            if ctx is not None:
                window._filter_timing_current_ctx = old_timing_ctx
        if ctx is not None:
            ctx["runtime_effects_ms"] = (time.perf_counter() - runtime_start) * 1000.0
            ctx.pop("defer_log", None)
            log_timing = getattr(window, "_filter_timing_log", None)
            if callable(log_timing):
                log_timing(ctx)

    def compact_int_setting_entry(key, label, tooltip=None):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_hexpand(True)
        row.set_halign(Gtk.Align.FILL)

        entry = Gtk.Entry()
        entry.add_css_class("sidebar-compact-entry")
        entry.set_width_chars(3)
        entry.set_max_width_chars(3)
        entry.set_hexpand(False)
        try:
            entry.set_alignment(0.95)
        except Exception:
            pass
        entry.set_text(str(window.settings.get(key, "") or ""))
        try:
            entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        except Exception:
            pass

        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_hexpand(True)
        lbl.set_halign(Gtk.Align.FILL)

        if tooltip:
            row.set_tooltip_text(tooltip)
            entry.set_tooltip_text(tooltip)
            lbl.set_tooltip_text(tooltip)

        def on_changed(_entry):
            if getattr(window, "_settings_update_guard", False):
                return
            val_s = (entry.get_text() or "").strip()
            try:
                val_i = int(val_s)
            except Exception:
                return
            window.settings[key] = val_i
            apply_sidebar_setting(key)

        entry.connect("changed", on_changed)
        window._sidebar_settings_widgets[key] = entry
        row.append(entry)
        row.append(lbl)
        return row

    def sidebar_setting_checkbutton(key, label, tooltip=None):
        row, cb = sidebar_mini_toggle_row(label, bool(window.settings.get(key, False)), tooltip=tooltip)

        def on_toggled(_cb):
            if getattr(window, "_settings_update_guard", False):
                return
            window.settings[key] = bool(cb.get_active())
            apply_sidebar_setting(key)

        cb.connect("toggled", on_toggled)
        window._sidebar_settings_widgets[key] = cb
        return row

    default_filters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    default_filters.set_hexpand(True)
    default_filters.set_halign(Gtk.Align.FILL)
    default_filters.append(sidebar_setting_checkbutton("pin_favorite_servers", "Pin Favourites"))
    default_filters.append(sidebar_setting_checkbutton(
        "prioritise_trusted_servers",
        "Trusted First",
        tooltip="Prioritise trusted servers:\nTop 100 first\nTop 1,000 next\nTop 2,000 next\nAll others after",
    ))
    default_filters.append(compact_int_setting_entry("high_ping_cutoff_ms", "Max Ping Cutoff (ms)"))
    default_filters.append(compact_int_setting_entry(
        "hide_below_max_players",
        "Min Player Slots",
        tooltip="Hide servers with fewer player slots than this.\nExample: 10 hides servers with max players below 10.",
    ))
    sidebar.append(default_filters)

    sidebar.append(hr())

    bottom_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    bottom_section.set_hexpand(True)
    bottom_section.set_vexpand(False)
    bottom_section.set_halign(Gtk.Align.FILL)
    bottom_section.set_valign(Gtk.Align.END)

    SIDEBAR_LOGO_BMC_SPACING = 0
    SIDEBAR_LOGO_WIDTH = min(186, max(1, SIDEBAR_WIDTH - (SIDEBAR_INNER_PADDING * 2)))
    SIDEBAR_SUPPORT_DISCLAIMER_SPACING = 10

    logo_bmc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SIDEBAR_LOGO_BMC_SPACING)
    logo_bmc_box.set_hexpand(False)
    logo_bmc_box.set_vexpand(False)
    logo_bmc_box.set_halign(Gtk.Align.CENTER)
    logo_bmc_box.set_valign(Gtk.Align.START)
    logo_bmc_has_child = False

    icon_path = os.path.join(IMAGES_DIR, "dzll-new-logo.png")
    if os.path.exists(icon_path):
        icon_pixbuf = GdkPixbuf.Pixbuf.new_from_file(icon_path)
        try:
            logo_height = max(1, round(SIDEBAR_LOGO_WIDTH * icon_pixbuf.get_height() / icon_pixbuf.get_width()))
        except Exception:
            logo_height = SIDEBAR_LOGO_WIDTH

        logo_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        logo_box.set_size_request(SIDEBAR_LOGO_WIDTH, logo_height)
        logo_box.set_hexpand(False)
        logo_box.set_vexpand(False)
        logo_box.set_halign(Gtk.Align.CENTER)
        logo_box.set_valign(Gtk.Align.CENTER)

        def draw_sidebar_icon(_area, cr, width, height):
            pixbuf_width = icon_pixbuf.get_width()
            pixbuf_height = icon_pixbuf.get_height()
            if pixbuf_width <= 0 or pixbuf_height <= 0 or width <= 0 or height <= 0:
                return
            scale = min(width / pixbuf_width, height / pixbuf_height)
            draw_width = pixbuf_width * scale
            draw_height = pixbuf_height * scale
            x = (width - draw_width) / 2
            y = (height - draw_height) / 2
            cr.save()
            cr.translate(x, y)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, icon_pixbuf, 0, 0)
            cr.paint()
            cr.restore()

        logo_icon = Gtk.DrawingArea()
        logo_icon.set_content_width(SIDEBAR_LOGO_WIDTH)
        logo_icon.set_content_height(logo_height)
        logo_icon.set_draw_func(draw_sidebar_icon)
        logo_icon.set_size_request(SIDEBAR_LOGO_WIDTH, logo_height)
        logo_icon.set_hexpand(False)
        logo_icon.set_vexpand(False)
        logo_icon.set_halign(Gtk.Align.CENTER)
        logo_icon.set_valign(Gtk.Align.CENTER)
        logo_box.append(logo_icon)

        logo_bmc_box.append(logo_box)
        logo_bmc_has_child = True
    else:
        print(f"[DZLL] Sidebar icon missing: {icon_path}")

    # --- Buy Me A Coffee button (sidebar, under logo) ---
    try:
        bmc_path = os.path.join(IMAGES_DIR, "buy-coffee.png")
        if os.path.exists(bmc_path):
            bmc_btn = Gtk.Button()
            bmc_btn.set_can_focus(False)
            bmc_btn.add_css_class("flat")
            bmc_btn.set_margin_top(10)
            attach_pointer_cursor(bmc_btn)
            bmc_btn.set_tooltip_text("Support DZLL (Buy Me A Coffee)")

            pic = Gtk.Picture.new_for_filename(bmc_path)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(False)
            pic.set_halign(Gtk.Align.CENTER)
            pic.set_valign(Gtk.Align.START)

            bmc_btn.set_child(pic)

            def _open_bmc(*_a):
                try:
                    Gio.AppInfo.launch_default_for_uri("https://buymeacoffee.com/berry.dingle", None)
                except Exception as e:
                    print(f"[BMC] Failed to open link: {e}")

            bmc_btn.connect("clicked", _open_bmc)
            logo_bmc_box.append(bmc_btn)
            logo_bmc_has_child = True
    except Exception:
        pass
    # -----------------------------------------------

    push = Gtk.Box()
    push.set_vexpand(True)
    sidebar.append(push)

    if logo_bmc_has_child:
        bottom_section.append(logo_bmc_box)

    gap = Gtk.Box()
    gap.set_size_request(-1, SIDEBAR_SUPPORT_DISCLAIMER_SPACING)
    bottom_section.append(gap)

    disclaimer = Gtk.Label(label=DISCLAIMER_TEXT)
    disclaimer.set_wrap(True)
    disclaimer.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    disclaimer.set_xalign(0)
    disclaimer.set_halign(Gtk.Align.FILL)
    disclaimer.set_hexpand(True)
    effective = SIDEBAR_WIDTH - (SIDEBAR_INNER_PADDING * 2)
    disclaimer.set_size_request(effective, -1)
    disclaimer.add_css_class("disclaimer")
    bottom_section.append(disclaimer)
    sidebar.append(bottom_section)

    return sidebar_frame
