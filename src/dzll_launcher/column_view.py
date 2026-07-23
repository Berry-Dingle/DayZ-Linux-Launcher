#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango, GLib, Gdk

from .ui_row import (
    PING_MARKUP_COLORS,
    ServerObject,
    attach_pointer_cursor,
    flag_for,
    row_ping_display,
    row_players_display,
    row_stable_display,
    row_time_display,
)


_META_WIDTHS = {
    "fav": 44,
    "time": 98,
    "played": 112,
    "map": 170,
    "players": 132,
    "ping": 90,
    "watch": 40,
    "download": 40,
    "join": 50,
}

_DUMP_COLUMNVIEW_TREE = os.environ.get("DZLL_DUMP_COLUMNVIEW_TREE") == "1"
_DEBUG_COLUMN_SORT = os.environ.get("DZLL_DEBUG_COLUMN_SORT") == "1"
_SORT_DEBUG_BIND_HOOK = None
_SORTABLE_HEADER_KEYS = {
    "PLAYED": "played",
    "PLAYERS": "players",
    "PING": "ping",
}
_REQUIRED_MODS_TWO_COLUMN_THRESHOLD = 10
_REQUIRED_MODS_POPOVER_MAX_HEIGHT = 360
_REQUIRED_MODS_POPOVER_MAX_WIDTH = 620
_IPPORT_MIN_WIDTH_CHARS = 18
_MODS_BUTTON_WIDTH_CHARS = len("Mods: 999")
_OPEN_REQUIRED_MODS_WIDGET = None
_OPEN_REQUIRED_MODS_POPOVER = None
_REQUIRED_MODS_POPUP_SUPPRESSED = False
_REQUIRED_MODS_POPUP_SUPPRESSION_COUNT = 0


def _monitor_icon_name() -> str:
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            icon_theme = Gtk.IconTheme.get_for_display(display)
            if icon_theme.has_icon("view-visible-symbolic"):
                return "view-visible-symbolic"
    except Exception:
        pass
    return "view-reveal-symbolic"


def _download_icon_name() -> str:
    candidates = ("folder-download-symbolic", "document-save-symbolic", "go-down-symbolic")
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            icon_theme = Gtk.IconTheme.get_for_display(display)
            for candidate in candidates:
                if icon_theme.has_icon(candidate):
                    return candidate
    except Exception:
        pass
    return candidates[-1]


def set_sort_debug_bind_hook(hook) -> None:
    global _SORT_DEBUG_BIND_HOOK
    _SORT_DEBUG_BIND_HOOK = hook


def _record_sort_debug_bind(kind: str) -> None:
    hook = _SORT_DEBUG_BIND_HOOK
    if hook is None:
        return
    try:
        hook(kind)
    except Exception:
        pass


def _disconnect_notify_handlers(widget, perf_metrics=None, factory_name: str = "") -> None:
    obj = getattr(widget, "_dzll_notify_obj", None)
    hids = list(getattr(widget, "_dzll_notify_ids", ()) or ())
    if obj is not None:
        for hid in hids:
            try:
                obj.disconnect(hid)
                if perf_metrics is not None:
                    perf_metrics.count("notify_disconnects")
                    perf_metrics.count(f"{factory_name}_notify_disconnects")
            except Exception:
                pass
    widget._dzll_notify_obj = None
    widget._dzll_notify_ids = []


def _notify_is_current(widget, obj) -> bool:
    return getattr(widget, "_dzll_notify_obj", None) is obj


def _drag_light_active(drag_light) -> bool:
    return bool(drag_light is not None and drag_light.is_active())


def _register_bound_cell(drag_light, factory_name, cell, list_item, full_refresh) -> None:
    if drag_light is not None and cell is not None:
        drag_light.register(
            factory_name,
            cell,
            list_item,
            list_item.get_item(),
            full_refresh,
        )


def _unregister_bound_cell(drag_light, cell, list_item=None) -> None:
    if drag_light is not None and cell is not None:
        drag_light.unregister(cell, list_item)


def _set_text_if_changed(widget, text: str, perf_metrics=None) -> bool:
    try:
        if widget.get_text() == text:
            if perf_metrics is not None:
                perf_metrics.count("drag_skipped_text_writes")
            return False
    except Exception:
        pass
    widget.set_text(text)
    return True


def _set_visible_if_changed(widget, visible: bool, perf_metrics=None) -> bool:
    try:
        if bool(widget.get_visible()) == bool(visible):
            if perf_metrics is not None:
                perf_metrics.count("drag_skipped_visibility_writes")
            return False
    except Exception:
        pass
    widget.set_visible(bool(visible))
    return True


def _add_css_classes(widget, css_classes) -> None:
    if not css_classes:
        return
    if isinstance(css_classes, str):
        css_classes = (css_classes,)
    for css_class in css_classes:
        if not css_class:
            continue
        try:
            widget.add_css_class(css_class)
        except Exception:
            pass


def _safe_widget_value(widget, method_name: str):
    method = getattr(widget, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


def _widget_type_name(widget) -> str:
    gtype = getattr(widget, "__gtype__", None)
    name = getattr(gtype, "name", None)
    if name:
        return str(name)
    return type(widget).__name__


def _format_css_classes(widget) -> str:
    classes = _safe_widget_value(widget, "get_css_classes")
    if not classes:
        return ""
    try:
        return "." + ".".join(str(css_class) for css_class in classes)
    except Exception:
        return ""


def _iter_widget_children(widget):
    get_first_child = getattr(widget, "get_first_child", None)
    if not callable(get_first_child):
        return
    try:
        child = get_first_child()
    except Exception:
        return
    while child is not None:
        yield child
        get_next_sibling = getattr(child, "get_next_sibling", None)
        if not callable(get_next_sibling):
            break
        try:
            child = get_next_sibling()
        except Exception:
            break


def _find_first_descendant(widget, predicate):
    for child in _iter_widget_children(widget):
        if predicate(child):
            return child
        found = _find_first_descendant(child, predicate)
        if found is not None:
            return found
    return None


def _css_name_is(widget, css_name: str) -> bool:
    return _safe_widget_value(widget, "get_css_name") == css_name


def _dump_widget_tree(widget, depth: int = 0) -> None:
    indent = "  " * depth
    type_name = _widget_type_name(widget)
    css_name = _safe_widget_value(widget, "get_css_name")
    css_classes = _format_css_classes(widget)
    visible = _safe_widget_value(widget, "get_visible")
    realized = _safe_widget_value(widget, "get_realized")
    mapped = _safe_widget_value(widget, "get_mapped")

    attrs = []
    if css_name:
        attrs.append(f"css={css_name}")
    if css_classes:
        attrs.append(f"classes={css_classes}")
    if visible is not None:
        attrs.append(f"visible={bool(visible)}")
    if realized is not None:
        attrs.append(f"realized={bool(realized)}")
    if mapped is not None:
        attrs.append(f"mapped={bool(mapped)}")

    suffix = f" [{' '.join(attrs)}]" if attrs else ""
    print(f"{indent}{type_name}{suffix}", flush=True)

    for child in _iter_widget_children(widget):
        _dump_widget_tree(child, depth + 1)


def _set_header_label_alignment(label: Gtk.Label, column_index: int) -> None:
    if column_index == 1:
        label.set_xalign(0.0)
        label.set_halign(Gtk.Align.START)
        label.set_margin_start(0)
        return

    label.set_xalign(0.5)
    label.set_halign(Gtk.Align.CENTER)
    label.set_hexpand(True)
    label.set_margin_start(0)


def _debug_column_sort(message: str) -> None:
    if _DEBUG_COLUMN_SORT:
        print(f"[COLUMN-SORT] {message}", flush=True)


def _column_header_title_for_button(button) -> str:
    label = _find_first_descendant(button, lambda widget: isinstance(widget, Gtk.Label))
    title = ""
    if label is not None:
        try:
            title = str(label.get_text() or "")
        except Exception:
            title = ""
    return title.replace("▲", "").replace("▼", "").strip().upper()


def _column_header_sort_key_for_title(view: Gtk.ColumnView, title: str) -> str | None:
    sortable_map = getattr(view, "_dzll_sortable_header_map", _SORTABLE_HEADER_KEYS)
    if title in sortable_map:
        return sortable_map[title]
    return None


def _attach_sort_header_gesture(view: Gtk.ColumnView, widget, sort_key: str) -> bool:
    if widget is None:
        return False
    if getattr(widget, "_dzll_sort_header_gesture_attached", False):
        return True

    gesture = Gtk.GestureClick.new()
    try:
        gesture.set_button(0)
    except Exception:
        pass
    try:
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    except Exception:
        pass

    def on_pressed(gesture_click, _n_press, _x, _y, key=sort_key, sort_view=view):
        try:
            if int(gesture_click.get_current_button()) != 1:
                return
        except Exception:
            pass

        now = time.monotonic()
        last_key, last_time = getattr(sort_view, "_dzll_sort_header_last_click", (None, 0.0))
        if last_key == key and now - float(last_time or 0.0) < 0.01:
            return
        sort_view._dzll_sort_header_last_click = (key, now)

        _debug_column_sort(f"header-click key={key}")
        handler = getattr(sort_view, "_dzll_on_sort_header_clicked", None)
        if callable(handler):
            handler(key)

    try:
        gesture.connect("pressed", on_pressed)
        widget.add_controller(gesture)
        widget._dzll_sort_header_gesture_attached = True
        widget._dzll_sort_header_key = sort_key
        return True
    except Exception:
        return False


def refresh_column_view_sort_header_handlers(view: Gtk.ColumnView) -> None:
    header = _find_first_descendant(view, lambda widget: _css_name_is(widget, "header"))
    callback = getattr(view, "_dzll_on_sort_header_clicked", None)
    callback_exists = callable(callback)
    title_buttons = []
    if header is not None:
        title_buttons = [child for child in _iter_widget_children(header) if _css_name_is(child, "button")]
    _debug_column_sort(f"refresh headers callback={callback_exists} buttons={len(title_buttons)}")
    if header is None or not callback_exists:
        return

    for column_index, button in enumerate(title_buttons):
        title = _column_header_title_for_button(button)
        sort_key = _column_header_sort_key_for_title(view, title)
        if not sort_key:
            _debug_column_sort(f"skip header index={column_index} title={title or '<empty>'} key=None attached=False")
            continue

        if not getattr(button, "_dzll_sort_header_pointer_attached", False):
            attach_pointer_cursor(button)
            button._dzll_sort_header_pointer_attached = True

        box = _find_first_descendant(button, lambda widget: _css_name_is(widget, "box"))
        label = _find_first_descendant(button, lambda widget: isinstance(widget, Gtk.Label))
        attached = False
        for target in (button, box, label):
            attached = _attach_sort_header_gesture(view, target, sort_key) or attached
        _debug_column_sort(f"attach header index={column_index} title={title} key={sort_key} attached={attached}")


def refresh_column_view_sort_indicators(view: Gtk.ColumnView, sort_key: str, sort_asc: bool) -> None:
    try:
        view._dzll_sort_key = str(sort_key or "")
        view._dzll_sort_asc = bool(sort_asc)
    except Exception:
        pass

    header = _find_first_descendant(view, lambda widget: _css_name_is(widget, "header"))
    if header is None:
        return

    title_buttons = [child for child in _iter_widget_children(header) if _css_name_is(child, "button")]
    for button in title_buttons:
        title = _column_header_title_for_button(button)
        key = _column_header_sort_key_for_title(view, title)
        if not key:
            continue

        label = _find_first_descendant(button, lambda widget: isinstance(widget, Gtk.Label))
        if label is None:
            continue

        text = title
        if key == sort_key:
            text = f"{title} {'▲' if sort_asc else '▼'}"
        try:
            label.set_text(text)
        except Exception:
            pass


def _normalize_column_view_header(view: Gtk.ColumnView) -> None:
    header = _find_first_descendant(view, lambda widget: _css_name_is(widget, "header"))
    if header is None:
        return

    if getattr(view, "_dzll_columnview_header_normalized", False):
        refresh_column_view_sort_header_handlers(view)
        refresh_column_view_sort_indicators(
            view,
            getattr(view, "_dzll_sort_key", ""),
            bool(getattr(view, "_dzll_sort_asc", False)),
        )
        return

    _add_css_classes(header, ("dzll-column-header-flat", "server-list-header-with-top-border"))
    try:
        header.remove_css_class("activatable")
    except Exception:
        pass

    title_buttons = [child for child in _iter_widget_children(header) if _css_name_is(child, "button")]
    if not title_buttons:
        return
    view._dzll_columnview_header_normalized = True

    for column_index, button in enumerate(title_buttons):
        _add_css_classes(button, "dzll-column-title-flat")
        try:
            button.set_halign(Gtk.Align.FILL)
            button.set_hexpand(True)
        except Exception:
            pass

        box = _find_first_descendant(button, lambda widget: _css_name_is(widget, "box"))
        if box is not None:
            try:
                box.set_halign(Gtk.Align.FILL)
                box.set_hexpand(True)
            except Exception:
                pass

        label = _find_first_descendant(button, lambda widget: isinstance(widget, Gtk.Label))
        if label is not None:
            _set_header_label_alignment(label, column_index)

    refresh_column_view_sort_header_handlers(view)
    refresh_column_view_sort_indicators(
        view,
        getattr(view, "_dzll_sort_key", ""),
        bool(getattr(view, "_dzll_sort_asc", False)),
    )


def _schedule_column_view_header_refresh(view: Gtk.ColumnView) -> None:
    def refresh_once():
        _normalize_column_view_header(view)
        refresh_column_view_sort_header_handlers(view)
        return False

    try:
        GLib.idle_add(refresh_once)
    except Exception:
        pass


def _install_column_view_header_normalizer(view: Gtk.ColumnView) -> None:
    def normalize_once():
        _normalize_column_view_header(view)
        _schedule_column_view_header_refresh(view)
        return False

    def on_map(_view):
        GLib.idle_add(normalize_once)

    def on_realize(_view):
        GLib.idle_add(normalize_once)

    try:
        view.connect("map", on_map)
    except Exception:
        GLib.idle_add(normalize_once)
    try:
        view.connect("realize", on_realize)
    except Exception:
        pass
    GLib.idle_add(normalize_once)


def _install_column_view_tree_dump(view: Gtk.ColumnView) -> None:
    if not _DUMP_COLUMNVIEW_TREE:
        return

    def dump_once():
        if getattr(view, "_dzll_columnview_tree_dumped", False):
            return False
        view._dzll_columnview_tree_dumped = True
        print("[DZLL] Gtk.ColumnView widget tree dump begin", flush=True)
        _dump_widget_tree(view)
        print("[DZLL] Gtk.ColumnView widget tree dump end", flush=True)
        return False

    def on_map(_view):
        GLib.idle_add(dump_once)

    try:
        view.connect("map", on_map)
    except Exception:
        GLib.idle_add(dump_once)


def _set_column_width(column, width: int | None, *, expand: bool = False) -> None:
    if width is not None:
        method_names = ("set_min_width",) if expand else ("set_fixed_width", "set_min_width")
        for method_name in method_names:
            method = getattr(column, method_name, None)
            if callable(method):
                try:
                    method(int(width))
                except Exception:
                    pass
    set_resizable = getattr(column, "set_resizable", None)
    if callable(set_resizable):
        try:
            set_resizable(bool(expand))
        except Exception:
            pass
    try:
        column.set_expand(bool(expand))
    except Exception:
        pass


def _set_column_resizable(column, resizable: bool) -> None:
    set_resizable = getattr(column, "set_resizable", None)
    if not callable(set_resizable):
        return
    try:
        set_resizable(bool(resizable))
    except Exception:
        pass


def _make_header_factory(title: str, *, xalign: float = 0.5, css_classes=None):
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, list_item):
        label = Gtk.Label(label=title, xalign=xalign)
        label.set_halign(Gtk.Align.FILL)
        label.set_hexpand(True)
        label.set_single_line_mode(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.add_css_class("dzll-column-header-label")
        _add_css_classes(label, css_classes)
        if xalign <= 0.0:
            label.set_margin_start(0)
        list_item.set_child(label)

    factory.connect("setup", setup)
    return factory


def _center_label(max_chars: int | None = None) -> Gtk.Label:
    label = Gtk.Label(xalign=0.5)
    label.set_halign(Gtk.Align.CENTER)
    label.set_valign(Gtk.Align.CENTER)
    label.set_hexpand(True)
    label.set_single_line_mode(True)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    if max_chars is not None:
        try:
            label.set_width_chars(int(max_chars))
            label.set_max_width_chars(int(max_chars))
        except Exception:
            pass
    return label


def _cell_label(widget) -> Gtk.Label | None:
    if isinstance(widget, Gtk.Label):
        return widget
    label = getattr(widget, "_dzll_cell_label", None)
    if isinstance(label, Gtk.Label):
        return label
    return None


def _bind_center_label(label: Gtk.Label, obj: ServerObject, binder) -> None:
    if isinstance(obj, ServerObject):
        binder(label, obj)
    else:
        label.set_text("")


def _make_label_factory(
    binder,
    *,
    factory_name: str,
    perf_metrics=None,
    drag_light=None,
    light_binder=None,
    notify_props=(),
    max_chars: int | None = None,
    cell_css_classes=None,
):
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, list_item):
        if perf_metrics is not None:
            perf_metrics.record_setup(factory_name)
        label = _center_label(max_chars=max_chars)
        if cell_css_classes:
            wrapper = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            wrapper.set_halign(Gtk.Align.FILL)
            wrapper.set_valign(Gtk.Align.FILL)
            wrapper.set_hexpand(True)
            wrapper.set_vexpand(True)
            _add_css_classes(wrapper, cell_css_classes)
            wrapper.append(label)
            wrapper._dzll_cell_label = label
            list_item.set_child(wrapper)
        else:
            list_item.set_child(label)

    def render_full(list_item, known_cell=None):
        child = list_item.get_child()
        label = _cell_label(child)
        if label is None or (known_cell is not None and child is not known_cell):
            return
        _disconnect_notify_handlers(label, perf_metrics, factory_name)
        obj = list_item.get_item()
        label._dzll_bound_obj = obj if isinstance(obj, ServerObject) else None
        _bind_center_label(label, obj, binder)
        if perf_metrics is not None and factory_name == "ping":
            perf_metrics.count("ping_label_writes")
            if isinstance(obj, ServerObject):
                perf_metrics.count("ping_format_calls")
        if not isinstance(obj, ServerObject) or not notify_props:
            return
        hids = []

        def notify(changed_obj, _pspec, cell=label):
            if not _notify_is_current(cell, changed_obj):
                return
            binder(cell, changed_obj)
            if perf_metrics is not None and factory_name == "ping":
                perf_metrics.count("ping_format_calls")
                perf_metrics.count("ping_label_writes")

        for prop in notify_props:
            try:
                hids.append(obj.connect(f"notify::{prop}", notify))
                if perf_metrics is not None:
                    perf_metrics.count("notify_connects")
                    perf_metrics.count(f"{factory_name}_notify_connects")
            except Exception:
                pass
        label._dzll_notify_obj = obj
        label._dzll_notify_ids = hids

    def render_light(list_item, known_cell=None):
        child = list_item.get_child()
        label = _cell_label(child)
        if label is None or (known_cell is not None and child is not known_cell):
            return
        _disconnect_notify_handlers(label, perf_metrics, factory_name)
        obj = list_item.get_item()
        label._dzll_bound_obj = obj if isinstance(obj, ServerObject) else None
        if isinstance(obj, ServerObject):
            if light_binder is not None:
                light_binder(label, obj, perf_metrics)
            else:
                binder(label, obj)
        else:
            _set_text_if_changed(label, "", perf_metrics)
        if perf_metrics is not None and notify_props:
            perf_metrics.count("drag_skipped_notify_connects", len(notify_props))

    def bind(_factory, list_item):
        light = _drag_light_active(drag_light)
        token = (
            perf_metrics.start(
                factory_name,
                "bind",
                bind_mode="light" if light else "full",
            )
            if perf_metrics is not None
            else None
        )
        try:
            _record_sort_debug_bind("label")
            child = list_item.get_child()
            if child is None:
                return
            if light:
                render_light(list_item, child)
            else:
                render_full(list_item, child)
            _register_bound_cell(
                drag_light,
                factory_name,
                child,
                list_item,
                render_full,
            )
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    def unbind(_factory, list_item):
        token = perf_metrics.start(factory_name, "unbind") if perf_metrics is not None else None
        try:
            label = _cell_label(list_item.get_child())
            if label is not None:
                _unregister_bound_cell(drag_light, list_item.get_child(), list_item)
                _disconnect_notify_handlers(label, perf_metrics, factory_name)
                label._dzll_bound_obj = None
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)
    return factory


def _bind_time(label: Gtk.Label, obj: ServerObject) -> None:
    time_text, timewarp_text = row_time_display(obj)
    label.set_text(f"{time_text} {timewarp_text}".strip())


def _bind_time_light(label: Gtk.Label, obj: ServerObject, perf_metrics=None) -> None:
    time_text, timewarp_text = row_time_display(obj)
    _set_text_if_changed(label, f"{time_text} {timewarp_text}".strip(), perf_metrics)


def _bind_played(label: Gtk.Label, obj: ServerObject) -> None:
    label.set_text((getattr(obj, "played", "") or "").strip())


def _bind_played_light(label: Gtk.Label, obj: ServerObject, perf_metrics=None) -> None:
    _set_text_if_changed(label, (getattr(obj, "played", "") or "").strip(), perf_metrics)


def _bind_map(label: Gtk.Label, obj: ServerObject) -> None:
    label.set_text((getattr(obj, "map_name", "") or "").strip())


def _bind_map_light(label: Gtk.Label, obj: ServerObject, perf_metrics=None) -> None:
    _set_text_if_changed(label, (getattr(obj, "map_name", "") or "").strip(), perf_metrics)


def _bind_players_cell(cell: Gtk.Box, obj: ServerObject | None, perf_metrics=None) -> None:
    players_label = getattr(cell, "_dzll_players_label", None)
    queue_label = getattr(cell, "_dzll_queue_label", None)
    if not isinstance(players_label, Gtk.Label) or not isinstance(queue_label, Gtk.Label):
        return
    if not isinstance(obj, ServerObject):
        players_label.set_text("")
        queue_label.set_text("")
        if perf_metrics is not None:
            perf_metrics.count("players_label_writes", 2)
        return
    players_text, queue_text = row_players_display(obj)
    players_label.set_text(players_text)
    queue_label.set_text(queue_text or "--")
    if perf_metrics is not None:
        perf_metrics.count("players_format_calls")
        perf_metrics.count("players_label_writes", 2)


def _bind_players_cell_light(
    cell: Gtk.Box,
    obj: ServerObject | None,
    perf_metrics=None,
) -> None:
    players_label = getattr(cell, "_dzll_players_label", None)
    queue_label = getattr(cell, "_dzll_queue_label", None)
    if not isinstance(players_label, Gtk.Label) or not isinstance(queue_label, Gtk.Label):
        return
    if isinstance(obj, ServerObject):
        players_text, queue_text = row_players_display(obj)
        _set_text_if_changed(players_label, players_text, perf_metrics)
        _set_text_if_changed(queue_label, queue_text or "--", perf_metrics)
        if perf_metrics is not None:
            perf_metrics.count("players_format_calls")
    else:
        _set_text_if_changed(players_label, "", perf_metrics)
        _set_text_if_changed(queue_label, "", perf_metrics)


def _make_players_factory(
    *,
    ubuntu_geometry: bool = False,
    perf_metrics=None,
    drag_light=None,
):
    factory = Gtk.SignalListItemFactory()
    notify_props = ("players", "max_players", "queue")

    def setup(_factory, list_item):
        if perf_metrics is not None:
            perf_metrics.record_setup("players")
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.set_halign(Gtk.Align.FILL)
        outer.set_valign(Gtk.Align.FILL)
        outer.set_hexpand(True)
        outer.set_vexpand(True)
        outer.add_css_class("dzll-column-cell-right-border")

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        content.set_hexpand(not ubuntu_geometry)

        players_label = Gtk.Label(xalign=1.0)
        players_label.add_css_class("dzll-players")
        players_label.set_halign(Gtk.Align.END)
        players_label.set_single_line_mode(True)
        players_label.set_width_chars(7)
        players_label.set_max_width_chars(7)

        queue_label = Gtk.Label(xalign=1.0)
        queue_label.add_css_class("dzll-queue")
        queue_label.set_halign(Gtk.Align.END)
        queue_label.set_single_line_mode(True)
        queue_label.set_width_chars(5)
        queue_label.set_max_width_chars(5)
        queue_label.set_margin_end(4)

        content.append(players_label)
        content.append(queue_label)
        if ubuntu_geometry:
            start_spacer = Gtk.Box()
            start_spacer.set_hexpand(True)
            end_spacer = Gtk.Box()
            end_spacer.set_hexpand(True)
            outer.append(start_spacer)
            outer.append(content)
            outer.append(end_spacer)
        else:
            outer.append(content)
        outer._dzll_players_label = players_label
        outer._dzll_queue_label = queue_label
        list_item.set_child(outer)

    def render_full(list_item, known_cell=None):
        cell = list_item.get_child()
        if cell is None or (known_cell is not None and cell is not known_cell):
            return
        _disconnect_notify_handlers(cell, perf_metrics, "players")
        obj = list_item.get_item()
        cell._dzll_bound_obj = obj if isinstance(obj, ServerObject) else None
        _bind_players_cell(
            cell,
            obj if isinstance(obj, ServerObject) else None,
            perf_metrics,
        )
        if not isinstance(obj, ServerObject):
            return
        hids = []

        def notify(changed_obj, _pspec, widget=cell):
            if not _notify_is_current(widget, changed_obj):
                return
            _bind_players_cell(widget, changed_obj, perf_metrics)

        for prop in notify_props:
            try:
                hids.append(obj.connect(f"notify::{prop}", notify))
                if perf_metrics is not None:
                    perf_metrics.count("notify_connects")
                    perf_metrics.count("players_notify_connects")
            except Exception:
                pass
        cell._dzll_notify_obj = obj
        cell._dzll_notify_ids = hids

    def render_light(list_item, known_cell=None):
        cell = list_item.get_child()
        if cell is None or (known_cell is not None and cell is not known_cell):
            return
        _disconnect_notify_handlers(cell, perf_metrics, "players")
        obj = list_item.get_item()
        cell._dzll_bound_obj = obj if isinstance(obj, ServerObject) else None
        _bind_players_cell_light(
            cell,
            obj if isinstance(obj, ServerObject) else None,
            perf_metrics,
        )
        if perf_metrics is not None:
            perf_metrics.count("drag_skipped_notify_connects", len(notify_props))

    def bind(_factory, list_item):
        light = _drag_light_active(drag_light)
        token = (
            perf_metrics.start(
                "players",
                "bind",
                bind_mode="light" if light else "full",
            )
            if perf_metrics is not None
            else None
        )
        try:
            _record_sort_debug_bind("players")
            cell = list_item.get_child()
            if cell is None:
                return
            if light:
                render_light(list_item, cell)
            else:
                render_full(list_item, cell)
            _register_bound_cell(
                drag_light,
                "players",
                cell,
                list_item,
                render_full,
            )
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    def unbind(_factory, list_item):
        token = perf_metrics.start("players", "unbind") if perf_metrics is not None else None
        try:
            cell = list_item.get_child()
            if cell is not None:
                _unregister_bound_cell(drag_light, cell, list_item)
                _disconnect_notify_handlers(cell, perf_metrics, "players")
                if _drag_light_active(drag_light):
                    _bind_players_cell_light(cell, None, perf_metrics)
                else:
                    _bind_players_cell(cell, None, perf_metrics)
                cell._dzll_bound_obj = None
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)
    return factory


def _bind_ping(label: Gtk.Label, obj: ServerObject) -> None:
    text, css_class = row_ping_display(obj)
    color = PING_MARKUP_COLORS.get(css_class)
    if color:
        label.set_markup(f'<span foreground="{color}">{text}</span>')
    else:
        label.set_text(text)


def _bind_ping_light(label: Gtk.Label, obj: ServerObject, perf_metrics=None) -> None:
    text, css_class = row_ping_display(obj)
    color = PING_MARKUP_COLORS.get(css_class)
    display_key = (text, css_class)
    current_text = None
    try:
        current_text = label.get_text()
    except Exception:
        pass
    if (
        current_text == text
        and getattr(label, "_dzll_ping_light_display", None) == display_key
    ):
        if perf_metrics is not None:
            perf_metrics.count("drag_skipped_text_writes")
    else:
        if color:
            label.set_markup(f'<span foreground="{color}">{text}</span>')
        else:
            label.set_text(text)
        label._dzll_ping_light_display = display_key
        if perf_metrics is not None:
            perf_metrics.count("light_ping_colour_updates")
    if perf_metrics is not None:
        perf_metrics.count("ping_format_calls")


def _bind_fav_button(button: Gtk.Button, obj: ServerObject | None, perf_metrics=None) -> None:
    button._dzll_bound_obj = obj
    if perf_metrics is not None:
        perf_metrics.count("fav_bound_assignments")
    fav = bool(getattr(obj, "fav", False)) if isinstance(obj, ServerObject) else False
    button._dzll_fav_state = fav
    label = getattr(button, "_dzll_label", None)
    if label is not None:
        label.set_markup('<span foreground="#f5c542">★</span>' if fav else '<span foreground="#7a7a7a">☆</span>')
        if perf_metrics is not None:
            perf_metrics.count("fav_label_writes")


def _bind_fav_button_light(
    button: Gtk.Button,
    obj: ServerObject | None,
    perf_metrics=None,
) -> None:
    button._dzll_bound_obj = obj
    if perf_metrics is not None:
        perf_metrics.count("fav_bound_assignments")
    fav = bool(getattr(obj, "fav", False)) if isinstance(obj, ServerObject) else False
    if getattr(button, "_dzll_fav_state", None) is fav:
        if perf_metrics is not None:
            perf_metrics.count("drag_skipped_text_writes")
        return
    label = getattr(button, "_dzll_label", None)
    if label is not None:
        label.set_markup(
            '<span foreground="#f5c542">★</span>'
            if fav
            else '<span foreground="#7a7a7a">☆</span>'
        )
        if perf_metrics is not None:
            perf_metrics.count("fav_label_writes")
    button._dzll_fav_state = fav


def _make_fav_factory(on_toggle_fav, perf_metrics=None, drag_light=None):
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, list_item):
        if perf_metrics is not None:
            perf_metrics.record_setup("fav")
        button = Gtk.Button()
        button.set_can_focus(False)
        button.set_halign(Gtk.Align.CENTER)
        button.set_valign(Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.add_css_class("dzll-column-fav-button")
        button.set_size_request(_META_WIDTHS["fav"], -1)
        label = Gtk.Label()
        label.add_css_class("dzll-column-fav-star")
        if perf_metrics is not None:
            perf_metrics.count("fav_css_changes", 3)
        button._dzll_label = label
        button.set_child(label)
        attach_pointer_cursor(button)

        def clicked(btn):
            obj = getattr(btn, "_dzll_bound_obj", None)
            if isinstance(obj, ServerObject) and callable(on_toggle_fav):
                on_toggle_fav(obj)

        button.connect("clicked", clicked)
        list_item.set_child(button)

    def render_full(list_item, known_cell=None):
        button = list_item.get_child()
        if button is None or (known_cell is not None and button is not known_cell):
            return
        _disconnect_notify_handlers(button, perf_metrics, "fav")
        obj = list_item.get_item()
        _bind_fav_button(
            button,
            obj if isinstance(obj, ServerObject) else None,
            perf_metrics,
        )
        if not isinstance(obj, ServerObject):
            return

        def notify(changed_obj, _pspec, btn=button):
            if not _notify_is_current(btn, changed_obj):
                return
            _bind_fav_button(btn, changed_obj, perf_metrics)

        try:
            hid = obj.connect("notify::fav", notify)
            button._dzll_notify_obj = obj
            button._dzll_notify_ids = [hid]
            if perf_metrics is not None:
                perf_metrics.count("notify_connects")
                perf_metrics.count("fav_notify_connects")
        except Exception:
            pass

    def render_light(list_item, known_cell=None):
        button = list_item.get_child()
        if button is None or (known_cell is not None and button is not known_cell):
            return
        _disconnect_notify_handlers(button, perf_metrics, "fav")
        obj = list_item.get_item()
        _bind_fav_button_light(
            button,
            obj if isinstance(obj, ServerObject) else None,
            perf_metrics,
        )
        if perf_metrics is not None:
            perf_metrics.count("drag_skipped_notify_connects")

    def bind(_factory, list_item):
        light = _drag_light_active(drag_light)
        token = (
            perf_metrics.start(
                "fav",
                "bind",
                bind_mode="light" if light else "full",
            )
            if perf_metrics is not None
            else None
        )
        try:
            _record_sort_debug_bind("fav")
            button = list_item.get_child()
            if button is None:
                return
            if light:
                render_light(list_item, button)
            else:
                render_full(list_item, button)
            _register_bound_cell(
                drag_light,
                "fav",
                button,
                list_item,
                render_full,
            )
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    def unbind(_factory, list_item):
        token = perf_metrics.start("fav", "unbind") if perf_metrics is not None else None
        try:
            button = list_item.get_child()
            if button is not None:
                _unregister_bound_cell(drag_light, button, list_item)
                _disconnect_notify_handlers(button, perf_metrics, "fav")
                button._dzll_bound_obj = None
                if perf_metrics is not None:
                    perf_metrics.count("fav_bound_assignments")
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)
    return factory


def _set_perspective_class(label: Gtk.Label, css_class: str, perf_metrics=None) -> None:
    previous = getattr(label, "_dzll_perspective_class", None)
    if previous == css_class:
        return
    for cls in ("perspective-badge-1pp", "perspective-badge-3pp"):
        try:
            label.remove_css_class(cls)
            if perf_metrics is not None:
                perf_metrics.count("name_css_changes")
        except Exception:
            pass
    label.add_css_class(css_class)
    if perf_metrics is not None:
        perf_metrics.count("name_css_changes")
    label._dzll_perspective_class = css_class


def _required_mod_names_from_json(mods_json: str) -> list[str]:
    if not mods_json:
        return []
    try:
        arr = json.loads(mods_json)
    except Exception:
        return []
    if not isinstance(arr, list):
        return []

    names: list[str] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            mod_id = str(item.get("steamWorkshopId") or "").strip()
            name = mod_id
        if name:
            names.append(name)
    return names


def _format_required_mod_display_name(name: str) -> str:
    """Capitalise word starts without changing any other characters."""
    formatted: list[str] = []
    capitalise_next = True
    for character in name:
        if character.isalpha():
            formatted.append(character.upper() if capitalise_next else character)
            capitalise_next = False
        else:
            formatted.append(character)
            if not character.isdigit():
                capitalise_next = True
    return "".join(formatted)


def _prepare_required_mod_display_names(names: list[str]) -> list[str]:
    formatted = [_format_required_mod_display_name(name) for name in names]
    return sorted(formatted, key=lambda name: (name.casefold(), name))


def _split_required_mod_display_names(names: list[str]) -> list[list[str]]:
    prepared = _prepare_required_mod_display_names(names)
    if len(prepared) <= _REQUIRED_MODS_TWO_COLUMN_THRESHOLD:
        return [prepared]
    split_at = (len(prepared) + 1) // 2
    return [prepared[:split_at], prepared[split_at:]]


def _make_required_mods_column(names: list[str]) -> Gtk.Box:
    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    column.set_valign(Gtk.Align.START)
    column.set_hexpand(True)
    for name in names:
        label = Gtk.Label(label=name, xalign=0.0)
        label.set_halign(Gtk.Align.FILL)
        label.set_hexpand(True)
        label.set_wrap(False)
        label.set_single_line_mode(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        try:
            label.set_width_chars(28)
            label.set_max_width_chars(34)
        except Exception:
            pass
        label.add_css_class("required-mods-popover-item")
        column.append(label)
    return column


def _make_required_mods_popover_content(names: list[str]) -> Gtk.Widget:
    columns = _split_required_mod_display_names(names)
    if len(columns) == 1:
        content = _make_required_mods_column(columns[0])
    else:
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        content.set_halign(Gtk.Align.FILL)
        content.set_valign(Gtk.Align.START)
        content.append(_make_required_mods_column(columns[0]))
        content.append(_make_required_mods_column(columns[1]))

    content.add_css_class("required-mods-popover-content")

    scroller = Gtk.ScrolledWindow()
    scroller.set_child(content)
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    try:
        scroller.set_propagate_natural_width(True)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_width(_REQUIRED_MODS_POPOVER_MAX_WIDTH)
        scroller.set_max_content_height(_REQUIRED_MODS_POPOVER_MAX_HEIGHT)
    except Exception:
        pass
    scroller.add_css_class("required-mods-popover-scroller")
    return scroller


def _widget_is_or_contains(widget, descendant) -> bool:
    current = descendant
    while current is not None:
        if current is widget:
            return True
        try:
            current = current.get_parent()
        except Exception:
            return False
    return False


def _pick_root_widget(root, x: float, y: float):
    pick = getattr(root, "pick", None)
    if not callable(pick):
        return None
    try:
        return pick(x, y, Gtk.PickFlags.DEFAULT)
    except Exception:
        try:
            return pick(x, y, 0)
        except Exception:
            return None


def _clear_open_required_mods_popover(widget=None, popover=None) -> None:
    global _OPEN_REQUIRED_MODS_WIDGET, _OPEN_REQUIRED_MODS_POPOVER
    if widget is not None and _OPEN_REQUIRED_MODS_WIDGET is not widget:
        return
    if popover is not None and _OPEN_REQUIRED_MODS_POPOVER is not popover:
        return
    _OPEN_REQUIRED_MODS_WIDGET = None
    _OPEN_REQUIRED_MODS_POPOVER = None


def _close_open_required_mods_popover() -> None:
    widget = _OPEN_REQUIRED_MODS_WIDGET
    if widget is not None:
        _popdown_required_mods_popover(widget)
    else:
        _clear_open_required_mods_popover()


def set_required_mods_popup_suppressed(suppressed: bool) -> bool:
    """Gate popup presentation without touching the Mods pill itself."""
    global _REQUIRED_MODS_POPUP_SUPPRESSED
    suppressed = bool(suppressed)
    if _REQUIRED_MODS_POPUP_SUPPRESSED == suppressed:
        return False
    had_open_popover = _OPEN_REQUIRED_MODS_WIDGET is not None
    _REQUIRED_MODS_POPUP_SUPPRESSED = suppressed
    if suppressed:
        _close_open_required_mods_popover()
    return had_open_popover


def required_mods_popup_suppression_count() -> int:
    return int(_REQUIRED_MODS_POPUP_SUPPRESSION_COUNT)


def _on_required_mods_root_active_notify(root, _pspec) -> None:
    try:
        active = bool(root.get_property("is-active"))
    except Exception:
        return
    if not active:
        _close_open_required_mods_popover()


def _on_required_mods_root_click(_gesture, _n_press, x, y, root) -> None:
    widget = _OPEN_REQUIRED_MODS_WIDGET
    popover = _OPEN_REQUIRED_MODS_POPOVER
    if widget is None or popover is None:
        return

    picked = _pick_root_widget(root, x, y)
    if picked is not None and (_widget_is_or_contains(widget, picked) or _widget_is_or_contains(popover, picked)):
        return

    _popdown_required_mods_popover(widget)


def _ensure_required_mods_root_click_controller(widget) -> None:
    get_root = getattr(widget, "get_root", None)
    if not callable(get_root):
        return
    try:
        root = get_root()
    except Exception:
        root = None
    if root is None:
        return
    if getattr(root, "_dzll_required_mods_root_click_controller", None) is None:
        click = Gtk.GestureClick.new()
        try:
            click.set_button(0)
        except Exception:
            pass
        try:
            click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        except Exception:
            pass
        click.connect("pressed", _on_required_mods_root_click, root)
        try:
            root.add_controller(click)
            root._dzll_required_mods_root_click_controller = click
        except Exception:
            pass
    if getattr(root, "_dzll_required_mods_active_notify_connected", False):
        return
    try:
        root.connect("notify::is-active", _on_required_mods_root_active_notify)
        root._dzll_required_mods_active_notify_connected = True
    except Exception:
        pass


def _popdown_required_mods_popover(widget) -> None:
    global _OPEN_REQUIRED_MODS_WIDGET, _OPEN_REQUIRED_MODS_POPOVER
    popover = getattr(widget, "_dzll_required_mods_popover", None)
    if popover is not None:
        try:
            popover.popdown()
        except Exception:
            pass
    if _OPEN_REQUIRED_MODS_WIDGET is widget:
        _OPEN_REQUIRED_MODS_WIDGET = None
        _OPEN_REQUIRED_MODS_POPOVER = None


def _set_required_mods_pointer_cursor(widget, enabled: bool) -> None:
    try:
        widget.set_cursor(Gdk.Cursor.new_from_name("pointer") if enabled else None)
    except Exception:
        pass


def _copy_ipport_label_address(label) -> bool:
    address = (getattr(label, "_dzll_ipport_plain", "") or "").strip()
    if not address:
        return False
    try:
        clipboard = label.get_display().get_clipboard()
        clipboard.set(address)
        return True
    except Exception:
        return False


def _show_ipport_copy_toast(label, x: float, y: float) -> None:
    try:
        root = label.get_root()
    except Exception:
        root = None
    show_toast = getattr(root, "_show_browser_toast", None)
    if callable(show_toast):
        try:
            show_toast("IP copied", source_widget=label, x=x, y=y)
        except Exception:
            pass


def _on_ipport_label_pressed(gesture, _n_press, _x, _y, label) -> None:
    try:
        button = int(gesture.get_current_button())
    except Exception:
        button = 0
    if button != 3:
        return
    if _copy_ipport_label_address(label):
        _show_ipport_copy_toast(label, _x, _y)


def _show_required_mods_popover(widget) -> None:
    global _OPEN_REQUIRED_MODS_WIDGET, _OPEN_REQUIRED_MODS_POPOVER
    global _REQUIRED_MODS_POPUP_SUPPRESSION_COUNT
    if _REQUIRED_MODS_POPUP_SUPPRESSED:
        _REQUIRED_MODS_POPUP_SUPPRESSION_COUNT += 1
        return
    names = getattr(widget, "_dzll_required_mod_names", None) or []
    if not names:
        _popdown_required_mods_popover(widget)
        return

    content_key = tuple(names)
    popover = getattr(widget, "_dzll_required_mods_popover", None)
    if _OPEN_REQUIRED_MODS_WIDGET is widget and _OPEN_REQUIRED_MODS_POPOVER is popover:
        try:
            if popover is not None and popover.get_visible():
                _popdown_required_mods_popover(widget)
                return
        except Exception:
            _popdown_required_mods_popover(widget)
            return

    if popover is None:
        popover = Gtk.Popover()
        popover.add_css_class("required-mods-popover")
        popover.set_has_arrow(True)
        popover.set_autohide(False)
        widget._dzll_required_mods_popover = popover
        try:
            popover.connect("closed", lambda closed_popover, owner=widget: _clear_open_required_mods_popover(owner, closed_popover))
        except Exception:
            pass
        try:
            popover.set_parent(widget)
        except Exception:
            pass
    if getattr(popover, "_dzll_required_mods_key", None) != content_key:
        perf_metrics = getattr(widget, "_dzll_factory_perf_metrics", None)
        if perf_metrics is not None:
            perf_metrics.count("name_popover_content_work")
        popover.set_child(_make_required_mods_popover_content(list(names)))
        popover._dzll_required_mods_key = content_key

    if _OPEN_REQUIRED_MODS_WIDGET is not None and _OPEN_REQUIRED_MODS_WIDGET is not widget:
        _popdown_required_mods_popover(_OPEN_REQUIRED_MODS_WIDGET)
    _ensure_required_mods_root_click_controller(widget)
    try:
        popover.popup()
        _OPEN_REQUIRED_MODS_WIDGET = widget
        _OPEN_REQUIRED_MODS_POPOVER = popover
    except Exception:
        pass


def _bind_required_mods_popover_target(
    widget,
    obj: ServerObject | None,
    perf_metrics=None,
) -> None:
    if perf_metrics is not None:
        perf_metrics.count("name_mod_prepare_calls")
        perf_metrics.count("name_popover_owner_checks")
    names = _required_mod_names_from_json(getattr(obj, "mods_json", "") or "") if isinstance(obj, ServerObject) else []
    previous_names = tuple(getattr(widget, "_dzll_required_mod_names", None) or [])
    widget._dzll_required_mod_names = names
    has_mods = bool(names)
    _set_required_mods_pointer_cursor(widget, has_mods)
    try:
        widget.set_tooltip_text("View required mods" if has_mods else None)
        if perf_metrics is not None:
            perf_metrics.count("name_tooltip_writes")
    except Exception:
        pass
    if _REQUIRED_MODS_POPUP_SUPPRESSED:
        return
    if tuple(names) != previous_names:
        if perf_metrics is not None:
            perf_metrics.count("name_popover_popdowns")
        _popdown_required_mods_popover(widget)
    if not names:
        if perf_metrics is not None:
            perf_metrics.count("name_popover_popdowns")
        _popdown_required_mods_popover(widget)


def _bind_name_cell(cell: Gtk.Box, obj: ServerObject | None, perf_metrics=None) -> None:
    lock_label = cell._dzll_lock_label
    name_label = cell._dzll_name_label
    perspective_label = cell._dzll_perspective_label
    flag_label = cell._dzll_flag_label
    ipport_label = cell._dzll_ipport_label
    mods_label = cell._dzll_mods_label
    mods_button = cell._dzll_mods_button
    mods_button_label = cell._dzll_mods_button_label
    cell._dzll_bound_obj = obj if isinstance(obj, ServerObject) else None
    mods_button._dzll_bound_obj = cell._dzll_bound_obj
    cell._dzll_drag_tooltips_cleared = False

    if not isinstance(obj, ServerObject):
        lock_label.set_text("")
        name_label.set_text("")
        name_label.set_tooltip_text(None)
        perspective_label.set_text("")
        flag_label.set_text("")
        ipport_label.set_text("")
        ipport_label.set_tooltip_text(None)
        ipport_label._dzll_ipport_plain = ""
        mods_label.set_text("")
        mods_label.set_visible(False)
        mods_button_label.set_text("")
        mods_button.set_visible(False)
        _bind_required_mods_popover_target(mods_button, None, perf_metrics)
        if perf_metrics is not None:
            perf_metrics.count("name_text_writes", 7)
            perf_metrics.count("name_tooltip_writes", 2)
            perf_metrics.count("name_visibility_changes", 2)
        return

    lock_label.set_text("🔒" if bool(getattr(obj, "password", False)) else "")
    server_name = (getattr(obj, "name", "") or "").strip()
    name_label.set_text(server_name)
    name_label.set_tooltip_text(server_name or None)

    is_3p = bool(getattr(obj, "third_person", False))
    perspective_label.set_text("3P" if is_3p else "1P")
    _set_perspective_class(
        perspective_label,
        "perspective-badge-3pp" if is_3p else "perspective-badge-1pp",
        perf_metrics,
    )

    country = (getattr(obj, "country", "") or "").strip().upper()
    flag_label.set_text(flag_for(country) if len(country) == 2 else "")

    _flag, _country_name, ipport, _meta = row_stable_display(obj)
    mod_count = int(getattr(obj, "mod_count", 0) or 0)
    mods_text = f"Mods: {mod_count}"
    ipport_label.set_text(ipport)
    ipport_label._dzll_ipport_plain = ipport
    ipport_label.set_tooltip_text(f"Right-click to copy address: {ipport}" if ipport else None)
    mods_label.set_text(mods_text)
    mods_button_label.set_text(mods_text)
    has_mods = mod_count > 0
    mods_label.set_visible(not has_mods)
    mods_button.set_visible(has_mods)
    _bind_required_mods_popover_target(mods_button, obj, perf_metrics)
    if perf_metrics is not None:
        perf_metrics.count("name_text_writes", 7)
        perf_metrics.count("name_tooltip_writes", 2)
        perf_metrics.count("name_visibility_changes", 2)
        perf_metrics.count(
            "name_flag_address_format_calls",
            1 + int(len(country) == 2),
        )


def _bind_name_cell_light(
    cell: Gtk.Box,
    obj: ServerObject | None,
    perf_metrics=None,
) -> None:
    lock_label = cell._dzll_lock_label
    name_label = cell._dzll_name_label
    perspective_label = cell._dzll_perspective_label
    flag_label = cell._dzll_flag_label
    ipport_label = cell._dzll_ipport_label
    mods_label = cell._dzll_mods_label
    mods_button = cell._dzll_mods_button
    mods_button_label = cell._dzll_mods_button_label

    current_obj = obj if isinstance(obj, ServerObject) else None
    cell._dzll_bound_obj = current_obj
    mods_button._dzll_bound_obj = current_obj
    # Never retain popup data from a recycled server while presentation is
    # suppressed. The normal settle refresh prepares the current names once.
    mods_button._dzll_required_mod_names = []
    _set_required_mods_pointer_cursor(mods_button, False)

    if not getattr(cell, "_dzll_drag_tooltips_cleared", False):
        for widget in (name_label, ipport_label, mods_button):
            try:
                widget.set_tooltip_text(None)
            except Exception:
                pass
        cell._dzll_drag_tooltips_cleared = True

    if current_obj is None:
        values = ("", "", "", "", "", "", "")
        has_mods = False
        ipport = ""
    else:
        mod_count = int(getattr(current_obj, "mod_count", 0) or 0)
        mods_text = f"Mods: {mod_count}"
        raw_country = getattr(current_obj, "country", "") or ""
        country = raw_country.strip().upper()
        flag_text = ""
        if len(country) == 2:
            stable_key = getattr(current_obj, "_row_stable_cache_key", None)
            cached_flag = getattr(current_obj, "_row_flag_text", None)
            cache_matches = (
                isinstance(stable_key, tuple)
                and bool(stable_key)
                and stable_key[0] == raw_country
                and raw_country.upper() == country
                and cached_flag is not None
            )
            flag_text = cached_flag if cache_matches else flag_for(country)
        ipport = getattr(current_obj, "_row_ipport_plain", None)
        if ipport is None:
            stable_flag, _country_name, ipport, _meta = row_stable_display(current_obj)
            if len(country) == 2 and raw_country.upper() == country:
                flag_text = stable_flag
        values = (
            "🔒" if bool(getattr(current_obj, "password", False)) else "",
            (getattr(current_obj, "name", "") or "").strip(),
            "3P" if bool(getattr(current_obj, "third_person", False)) else "1P",
            flag_text,
            ipport,
            mods_text,
            mods_text,
        )
        has_mods = mod_count > 0

    for index, (widget, value) in enumerate(zip(
        (
            lock_label,
            name_label,
            perspective_label,
            flag_label,
            ipport_label,
            mods_label,
            mods_button_label,
        ),
        values,
    )):
        changed = _set_text_if_changed(widget, value, perf_metrics)
        if changed and index == 3 and perf_metrics is not None:
            perf_metrics.count("light_flag_updates")
    ipport_label._dzll_ipport_plain = ipport
    _set_visible_if_changed(mods_label, not has_mods, perf_metrics)
    _set_visible_if_changed(mods_button, has_mods, perf_metrics)
    if perf_metrics is not None:
        perf_metrics.count("drag_skipped_tooltip_writes", 3)
        perf_metrics.count("drag_skipped_css_writes", 3)
        perf_metrics.count("drag_skipped_popup_preparation")


def _make_name_factory(perf_metrics=None, drag_light=None):
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, list_item):
        if perf_metrics is not None:
            perf_metrics.record_setup("name")
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.set_hexpand(True)
        outer.set_vexpand(True)
        outer.set_halign(Gtk.Align.FILL)
        outer.set_valign(Gtk.Align.FILL)
        outer.add_css_class("dzll-column-cell-right-border")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        content.set_hexpand(True)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_start(6)
        content.set_margin_end(6)
        content.set_margin_top(2)
        content.set_margin_bottom(2)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        top.set_hexpand(True)
        top.set_halign(Gtk.Align.FILL)
        lock_label = Gtk.Label()
        lock_label.set_valign(Gtk.Align.CENTER)
        lock_label.set_halign(Gtk.Align.CENTER)
        lock_label.set_size_request(18, -1)
        name_label = Gtk.Label(xalign=0.0)
        name_label.set_hexpand(True)
        name_label.set_halign(Gtk.Align.FILL)
        name_label.set_single_line_mode(True)
        name_label.set_wrap(False)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        try:
            name_label.set_width_chars(1)
        except Exception:
            pass
        name_label.add_css_class("server-name")
        top.append(lock_label)
        top.append(name_label)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bottom.set_hexpand(False)
        bottom.set_halign(Gtk.Align.START)
        perspective_label = Gtk.Label(xalign=0.5)
        perspective_label.add_css_class("perspective-badge")
        perspective_label.set_valign(Gtk.Align.CENTER)
        flag_label = Gtk.Label()
        flag_label.set_valign(Gtk.Align.CENTER)
        ipport_label = Gtk.Label(xalign=0.0)
        ipport_label.set_hexpand(False)
        ipport_label.set_halign(Gtk.Align.START)
        ipport_label.set_single_line_mode(True)
        ipport_label.set_ellipsize(Pango.EllipsizeMode.END)
        try:
            ipport_label.set_width_chars(_IPPORT_MIN_WIDTH_CHARS)
            ipport_label.set_max_width_chars(_IPPORT_MIN_WIDTH_CHARS)
        except Exception:
            pass
        ipport_label.add_css_class("dim-label")
        ipport_click = Gtk.GestureClick.new()
        try:
            ipport_click.set_button(0)
        except Exception:
            pass
        ipport_click.connect("pressed", _on_ipport_label_pressed, ipport_label)
        ipport_label.add_controller(ipport_click)

        mods_label = Gtk.Label(xalign=0.0)
        mods_label.set_valign(Gtk.Align.CENTER)
        mods_label.set_hexpand(False)
        mods_label.set_halign(Gtk.Align.START)
        mods_label.set_single_line_mode(True)
        mods_label.set_ellipsize(Pango.EllipsizeMode.END)
        try:
            mods_label.set_width_chars(_MODS_BUTTON_WIDTH_CHARS)
            mods_label.set_max_width_chars(_MODS_BUTTON_WIDTH_CHARS)
        except Exception:
            pass
        mods_label.add_css_class("dim-label")
        mods_button = Gtk.Button()
        mods_button.set_valign(Gtk.Align.CENTER)
        mods_button.set_hexpand(False)
        mods_button.set_halign(Gtk.Align.START)
        mods_button.add_css_class("required-mods-popover-target")
        mods_button_label = Gtk.Label(xalign=0.5)
        mods_button_label.set_single_line_mode(True)
        mods_button_label.set_ellipsize(Pango.EllipsizeMode.END)
        try:
            mods_button_label.set_width_chars(_MODS_BUTTON_WIDTH_CHARS)
            mods_button_label.set_max_width_chars(_MODS_BUTTON_WIDTH_CHARS)
        except Exception:
            pass
        mods_button.set_child(mods_button_label)
        mods_button.connect("clicked", lambda button: _show_required_mods_popover(button))
        bottom.append(perspective_label)
        bottom.append(flag_label)
        bottom.append(ipport_label)
        bottom.append(mods_label)
        bottom.append(mods_button)

        content.append(top)
        content.append(bottom)
        outer.append(content)
        outer._dzll_lock_label = lock_label
        outer._dzll_name_label = name_label
        outer._dzll_perspective_label = perspective_label
        outer._dzll_flag_label = flag_label
        outer._dzll_ipport_label = ipport_label
        outer._dzll_mods_label = mods_label
        outer._dzll_mods_button = mods_button
        outer._dzll_mods_button_label = mods_button_label
        mods_button._dzll_factory_perf_metrics = perf_metrics
        if perf_metrics is not None:
            perf_metrics.count("name_css_changes", 5)
        list_item.set_child(outer)

    def render_full(list_item, known_cell=None):
        cell = list_item.get_child()
        if cell is None or (known_cell is not None and cell is not known_cell):
            return
        _bind_name_cell(cell, list_item.get_item(), perf_metrics)

    def render_light(list_item, known_cell=None):
        cell = list_item.get_child()
        if cell is None or (known_cell is not None and cell is not known_cell):
            return
        _bind_name_cell_light(cell, list_item.get_item(), perf_metrics)

    def bind(_factory, list_item):
        light = _drag_light_active(drag_light)
        token = (
            perf_metrics.start(
                "name",
                "bind",
                bind_mode="light" if light else "full",
            )
            if perf_metrics is not None
            else None
        )
        try:
            _record_sort_debug_bind("name")
            cell = list_item.get_child()
            if cell is None:
                return
            if light:
                render_light(list_item, cell)
            else:
                render_full(list_item, cell)
            _register_bound_cell(
                drag_light,
                "name",
                cell,
                list_item,
                render_full,
            )
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    def unbind(_factory, list_item):
        token = perf_metrics.start("name", "unbind") if perf_metrics is not None else None
        try:
            cell = list_item.get_child()
            _unregister_bound_cell(drag_light, cell, list_item)
            if _drag_light_active(drag_light):
                _bind_name_cell_light(cell, None, perf_metrics)
            else:
                _bind_name_cell(cell, None, perf_metrics)
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)
    return factory


def _bind_action_button_light(
    button,
    obj,
    *,
    factory_name: str,
    active_css_class: str | None,
    tooltip_text: str | None,
    perf_metrics=None,
) -> None:
    button._dzll_bound_obj = obj if isinstance(obj, ServerObject) else None
    if perf_metrics is not None:
        perf_metrics.count(f"{factory_name}_bound_assignments")
    if not active_css_class:
        return
    if not getattr(button, "_dzll_drag_action_light", False) and tooltip_text:
        button.set_tooltip_text(tooltip_text)
    button._dzll_drag_action_light = True
    if perf_metrics is not None:
        perf_metrics.count("drag_skipped_tooltip_writes")
        perf_metrics.count("drag_skipped_css_writes")
        perf_metrics.count("drag_skipped_image_writes", 2)


def _make_action_factory(
    icon_name: str,
    on_clicked,
    css_class: str | None = None,
    *,
    margin_start: int = 0,
    margin_end: int = 0,
    active_css_class: str | None = None,
    is_active=None,
    tooltip_text: str | None = None,
    active_tooltip_text: str | None = None,
    accessible_label: str | None = None,
    is_sensitive=None,
    presentation=None,
    factory_name: str,
    perf_metrics=None,
    drag_light=None,
):
    factory = Gtk.SignalListItemFactory()
    buttons = []

    def update_active_state(button, obj) -> None:
        button._dzll_drag_action_light = False
        sensitive = True
        if callable(is_sensitive):
            try:
                sensitive = bool(is_sensitive(obj)) if isinstance(obj, ServerObject) else False
            except Exception:
                sensitive = False
        button.set_sensitive(sensitive)
        dynamic = {}
        if isinstance(obj, ServerObject) and callable(presentation):
            try:
                dynamic = dict(presentation(obj) or {})
            except Exception:
                dynamic = {}
        dynamic_css = str(dynamic.get("css_class") or "")
        previous_css = str(getattr(button, "_dzll_dynamic_css_class", "") or "")
        if previous_css and previous_css != dynamic_css:
            button.remove_css_class(previous_css)
        if dynamic_css and dynamic_css != previous_css:
            button.add_css_class(dynamic_css)
        button._dzll_dynamic_css_class = dynamic_css
        image = button.get_child()
        dynamic_icon = str(dynamic.get("icon_name") or icon_name)
        if isinstance(image, Gtk.Image):
            image.set_from_icon_name(dynamic_icon)
        dynamic_tooltip = dynamic.get("tooltip")
        if dynamic_tooltip is not None:
            button.set_tooltip_text(str(dynamic_tooltip))
        elif tooltip_text:
            button.set_tooltip_text(tooltip_text)
        dynamic_accessible = dynamic.get("accessible_label")
        if dynamic_accessible is not None:
            try:
                button.update_property(
                    [Gtk.AccessibleProperty.LABEL], [str(dynamic_accessible)],
                )
            except Exception:
                pass
        if not active_css_class:
            return
        active = False
        if isinstance(obj, ServerObject) and callable(is_active):
            try:
                active = bool(is_active(obj))
            except Exception:
                active = False
        if active:
            button.add_css_class(active_css_class)
            if perf_metrics is not None:
                perf_metrics.count(f"{factory_name}_css_changes")
            if isinstance(image, Gtk.Image):
                image.remove_css_class("monitor-eye-idle")
                image.add_css_class("monitor-eye-active")
                if perf_metrics is not None:
                    perf_metrics.count(f"{factory_name}_image_state_changes", 2)
        else:
            button.remove_css_class(active_css_class)
            if perf_metrics is not None:
                perf_metrics.count(f"{factory_name}_css_changes")
            if isinstance(image, Gtk.Image):
                image.remove_css_class("monitor-eye-active")
                image.add_css_class("monitor-eye-idle")
                if perf_metrics is not None:
                    perf_metrics.count(f"{factory_name}_image_state_changes", 2)
        if dynamic_tooltip is None and (tooltip_text or active_tooltip_text):
            button.set_tooltip_text(active_tooltip_text if active and active_tooltip_text else tooltip_text)
            if perf_metrics is not None:
                perf_metrics.count(f"{factory_name}_tooltip_writes")

    def setup(_factory, list_item):
        if perf_metrics is not None:
            perf_metrics.record_setup(factory_name)
        button = Gtk.Button()
        buttons.append(button)
        button.set_can_focus(False)
        button.set_halign(Gtk.Align.CENTER)
        button.set_valign(Gtk.Align.CENTER)
        button.add_css_class("flat")
        if perf_metrics is not None:
            perf_metrics.count(f"{factory_name}_css_changes")
        if css_class:
            button.add_css_class(css_class)
            if perf_metrics is not None:
                perf_metrics.count(f"{factory_name}_css_changes")
        if margin_start:
            button.set_margin_start(margin_start)
        if margin_end:
            button.set_margin_end(margin_end)
        button.set_child(Gtk.Image.new_from_icon_name(icon_name))
        if perf_metrics is not None:
            perf_metrics.count(f"{factory_name}_image_state_changes")
        if tooltip_text:
            button.set_tooltip_text(tooltip_text)
            if perf_metrics is not None:
                perf_metrics.count(f"{factory_name}_tooltip_writes")
        if accessible_label:
            try:
                button.update_property(
                    [Gtk.AccessibleProperty.LABEL], [str(accessible_label)],
                )
            except Exception:
                pass
        button.set_size_request(34, -1)
        attach_pointer_cursor(button)

        def clicked(btn):
            obj = getattr(btn, "_dzll_bound_obj", None)
            if isinstance(obj, ServerObject) and callable(on_clicked):
                on_clicked(obj)

        button.connect("clicked", clicked)
        list_item.set_child(button)

    def render_full(list_item, known_cell=None):
        button = list_item.get_child()
        if button is None or (known_cell is not None and button is not known_cell):
            return
        obj = list_item.get_item()
        button._dzll_bound_obj = obj if isinstance(obj, ServerObject) else None
        if perf_metrics is not None:
            perf_metrics.count(f"{factory_name}_bound_assignments")
        update_active_state(button, button._dzll_bound_obj)

    def render_light(list_item, known_cell=None):
        button = list_item.get_child()
        if button is None or (known_cell is not None and button is not known_cell):
            return
        _bind_action_button_light(
            button,
            list_item.get_item(),
            factory_name=factory_name,
            active_css_class=active_css_class,
            tooltip_text=tooltip_text,
            perf_metrics=perf_metrics,
        )

    def bind(_factory, list_item):
        light = _drag_light_active(drag_light)
        token = (
            perf_metrics.start(
                factory_name,
                "bind",
                bind_mode="light" if light else "full",
            )
            if perf_metrics is not None
            else None
        )
        try:
            _record_sort_debug_bind("action")
            button = list_item.get_child()
            if button is None:
                return
            if light and not callable(presentation):
                render_light(list_item, button)
            else:
                render_full(list_item, button)
            _register_bound_cell(
                drag_light,
                factory_name,
                button,
                list_item,
                render_full,
            )
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    def unbind(_factory, list_item):
        token = perf_metrics.start(factory_name, "unbind") if perf_metrics is not None else None
        try:
            button = list_item.get_child()
            if button is not None:
                _unregister_bound_cell(drag_light, button, list_item)
                button._dzll_bound_obj = None
                if perf_metrics is not None:
                    perf_metrics.count(f"{factory_name}_bound_assignments")
                if _drag_light_active(drag_light):
                    if perf_metrics is not None and active_css_class:
                        perf_metrics.count("drag_skipped_tooltip_writes")
                        perf_metrics.count("drag_skipped_css_writes")
                        perf_metrics.count("drag_skipped_image_writes", 2)
                else:
                    update_active_state(button, None)
        finally:
            if perf_metrics is not None:
                perf_metrics.finish(token)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)

    def refresh_active_states() -> None:
        for button in list(buttons):
            update_active_state(button, getattr(button, "_dzll_bound_obj", None))

    factory._dzll_refresh_active_states = refresh_active_states
    return factory


def _append_column(
    view: Gtk.ColumnView,
    title: str,
    factory,
    width: int | None = None,
    *,
    expand: bool = False,
    header_title: str | None = None,
    header_xalign: float = 0.5,
    header_css_classes=None,
):
    column = Gtk.ColumnViewColumn.new(title, factory)
    set_header_factory = getattr(column, "set_header_factory", None)
    if callable(set_header_factory):
        try:
            set_header_factory(
                _make_header_factory(
                    title if header_title is None else header_title,
                    xalign=header_xalign,
                    css_classes=header_css_classes,
                )
            )
        except Exception:
            pass
    _set_column_width(column, width, expand=expand)
    view.append_column(column)
    return column


def build_server_column_view(
    selection_model,
    on_toggle_fav,
    on_monitor,
    on_download_mods,
    on_join,
    on_header_sort=None,
    is_monitored=None,
    can_download_mods=None,
    can_join=None,
    download_presentation=None,
    join_presentation=None,
    ubuntu_geometry: bool = False,
    perf_metrics=None,
    drag_light=None,
) -> tuple[Gtk.ColumnView, dict[str, bool]]:
    view = Gtk.ColumnView.new(selection_model)
    view._dzll_on_sort_header_clicked = on_header_sort
    view._dzll_sortable_header_map = dict(_SORTABLE_HEADER_KEYS)
    view.add_css_class("dzll-column-view")
    view.set_hexpand(True)
    view.set_vexpand(True)
    _install_column_view_header_normalizer(view)
    _install_column_view_tree_dump(view)

    features = {
        "column_separators": False,
        "row_separators": False,
    }
    try:
        view.set_show_column_separators(False)
        features["column_separators"] = True
    except Exception:
        pass
    try:
        view.set_show_row_separators(False)
        features["row_separators"] = True
    except Exception:
        pass

    _append_column(
        view,
        "FAV",
        _make_fav_factory(on_toggle_fav, perf_metrics, drag_light),
        _META_WIDTHS["fav"],
    )
    name_column = _append_column(
        view,
        "NAME / IP / MODS",
        _make_name_factory(perf_metrics, drag_light),
        320,
        expand=True,
        header_xalign=0.0,
        header_css_classes="dzll-column-header-name",
    )
    _set_column_resizable(name_column, False)

    right_border = "dzll-column-cell-right-border"

    _append_column(
        view,
        "TIME",
        _make_label_factory(_bind_time,
            factory_name="time",
            perf_metrics=perf_metrics,
            drag_light=drag_light,
            light_binder=_bind_time_light,
            max_chars=11,
            cell_css_classes=right_border,
        ),
        _META_WIDTHS["time"],
    )
    _append_column(
        view,
        "PLAYED",
        _make_label_factory(_bind_played,
            factory_name="played",
            perf_metrics=perf_metrics,
            drag_light=drag_light,
            light_binder=_bind_played_light,
            max_chars=12,
            cell_css_classes=right_border,
        ),
        _META_WIDTHS["played"],
    )
    _append_column(
        view,
        "MAP",
        _make_label_factory(_bind_map,
            factory_name="map",
            perf_metrics=perf_metrics,
            drag_light=drag_light,
            light_binder=_bind_map_light,
            max_chars=18,
            cell_css_classes=right_border,
        ),
        _META_WIDTHS["map"],
    )
    _append_column(
        view,
        "PLAYERS",
        _make_players_factory(
            ubuntu_geometry=ubuntu_geometry,
            perf_metrics=perf_metrics,
            drag_light=drag_light,
        ),
        _META_WIDTHS["players"],
    )
    _append_column(
        view,
        "PING",
        _make_label_factory(_bind_ping,
            factory_name="ping",
            perf_metrics=perf_metrics,
            drag_light=drag_light,
            light_binder=_bind_ping_light,
            notify_props=("ping",),
            max_chars=8,
            cell_css_classes=right_border,
        ),
        _META_WIDTHS["ping"],
    )
    monitor_factory = _make_action_factory(
        _monitor_icon_name(),
        on_monitor,
        "monitor-btn",
        margin_start=4,
        active_css_class="monitor-btn-active",
        is_active=is_monitored,
        tooltip_text="Monitor this server in\nthe Server Companion",
        active_tooltip_text="Currently monitoring this server",
        factory_name="monitor",
        perf_metrics=perf_metrics,
        drag_light=drag_light,
    )
    _append_column(
        view,
        "",
        monitor_factory,
        _META_WIDTHS["watch"],
        header_title="",
        header_css_classes="dzll-column-header-action",
    )
    view.refresh_monitor_highlights = getattr(monitor_factory, "_dzll_refresh_active_states", lambda: None)
    download_factory = _make_action_factory(
        _download_icon_name(),
        on_download_mods,
        "dzll-download-mods-button",
        tooltip_text="Subscribe to and download required mods\nwithout joining",
        accessible_label="Subscribe to and download required mods without joining",
        is_sensitive=can_download_mods,
        presentation=download_presentation,
        factory_name="download_mods",
        perf_metrics=perf_metrics,
        drag_light=drag_light,
    )
    _append_column(
        view,
        "",
        download_factory,
        _META_WIDTHS["download"],
        header_title="",
        header_css_classes="dzll-column-header-action",
    )
    view.refresh_download_mods_states = getattr(
        download_factory, "_dzll_refresh_active_states", lambda: None,
    )
    join_factory = _make_action_factory(
        "media-playback-start-symbolic",
        on_join,
        "dzll-join-button",
        margin_end=4,
        tooltip_text="Join this server",
        accessible_label="Join this server",
        is_sensitive=can_join,
        presentation=join_presentation,
        factory_name="join",
        perf_metrics=perf_metrics,
        drag_light=drag_light,
    )
    _append_column(
        view,
        "",
        join_factory,
        _META_WIDTHS["join"],
        header_title="",
        header_css_classes="dzll-column-header-action",
    )
    view.refresh_join_states = getattr(
        join_factory, "_dzll_refresh_active_states", lambda: None,
    )
    return view, features
