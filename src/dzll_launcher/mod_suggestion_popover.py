"""Mod-search session: the suggestion popover and the selected mod chips (``ModSuggestionPopover``).

The popover owns:

- the suggestion rows under the search entry: building, selection,
  keyboard navigation and scrolling;
- the token under the cursor and applying a chosen suggestion to it;
- the short debounce before a suggestion refresh, and its cancellation;
- Mod Search mode (the MOD toggle): the saved normal search text, the
  entry-update guard and the selected mod chips.

Suggestions come from the ``ModSuggestionCacheCoordinator`` catalogue. The
popover never filters the browser itself: chip changes and leaving Mod
Search call the injected search-filter callbacks, and the window's filter
core reads ``normal_filter_text()`` and ``chip_query_terms()``.

The widgets are built by ``sidebar_ui.build_search_area`` and handed over
with ``bind_widgets``. Every method runs on the GTK main thread.
"""

from __future__ import annotations

from typing import Any, Callable

from gi.repository import Gdk, GLib, Gtk, Pango

from .mod_search import compact_mod_text, normalize_mod_text, split_mod_search_operator
from .mod_suggestions import (
    SUGGESTION_SHORTHANDS,
    current_comma_token,
    replace_comma_token,
    suggest_mods,
)

MOD_SUGGESTION_RESULT_LIMIT = 50
SUGGESTION_REFRESH_DEBOUNCE_MS = 35


class ModSuggestionPopover:
    """Suggestion popover, Mod Search mode and selected chips for the search entry."""

    def __init__(
        self,
        *,
        catalogue,
        has_rows: Callable[[], Any],
        apply_search_filter: Callable[..., None],
        cancel_search_filter_debounce: Callable[[], None],
        debug_enabled: Callable[[], bool],
    ) -> None:
        self._catalogue = catalogue
        self._has_rows = has_rows
        self._apply_search_filter = apply_search_filter
        self._cancel_search_filter_debounce = cancel_search_filter_debounce
        self._debug_enabled = debug_enabled

        self._entry = None
        self._panel = None
        self._list = None
        self._scroller = None
        self._refresh_chips = None
        self._sync_mode_ui = None

        self.selecting = False
        self.dismissed = None
        self.refresh_source_id = 0
        self.last_rows = ()
        self.mode_active = False
        self.saved_normal_text = ""
        self.entry_update_guard = False
        self.selected_chips = []

    def bind_widgets(
        self,
        *,
        entry,
        panel,
        suggestion_list,
        scroller,
        refresh_chips: Callable[[], None] | None,
        sync_mode_ui: Callable[[bool], None] | None,
    ) -> None:
        """Attach the search-area widgets and chip/mode presentation built by ``sidebar_ui``."""
        self._entry = entry
        self._panel = panel
        self._list = suggestion_list
        self._scroller = scroller
        self._refresh_chips = refresh_chips
        self._sync_mode_ui = sync_mode_ui

    # ------------------------------------------------------------- filter inputs

    def normal_filter_text(self) -> str:
        entry = self._entry
        if not entry:
            return ""
        if bool(self.mode_active):
            return str(self.saved_normal_text or "")
        return entry.get_text() or ""

    def chip_query_terms(self) -> tuple[str, ...]:
        terms = []
        for chip in self.selected_chips or []:
            if not isinstance(chip, dict):
                continue
            term = str(chip.get("query_term") or "").strip()
            if term:
                terms.append(term)
        return tuple(terms)

    # ------------------------------------------------------------- chips and Mod Search mode

    def _refresh_chip_ui(self):
        refresh = self._refresh_chips
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass

    def _chip_key(self, chip: dict) -> str:
        workshop_id = str(chip.get("workshop_id") or "").strip()
        if workshop_id:
            return f"id:{workshop_id}"
        return f"name:{normalize_mod_text(chip.get('query_term') or chip.get('display_name') or '')}"

    def resolve_exact_chip(self, text: str) -> dict | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        catalogue = self._catalogue
        if (not bool(catalogue.ready) and
                (bool(catalogue.restore_pending) or bool(catalogue.restore_inflight))):
            return None
        normalized = normalize_mod_text(raw)
        compact = compact_mod_text(raw)
        index = catalogue.index
        mods = index.get("mods", ()) if isinstance(index, dict) else ()
        exact_matches = []
        for entry in mods or ():
            if not isinstance(entry, dict):
                continue
            values = [
                entry.get("display_name") or "",
                entry.get("name") or "",
                *(entry.get("raw_names", ()) or ()),
            ]
            for value in values:
                if normalize_mod_text(value) == normalized or compact_mod_text(value) == compact:
                    exact_matches.append(entry)
                    break
        if not exact_matches:
            suggestions = suggest_mods(index, raw, limit=1)
            if suggestions:
                candidate = suggestions[0]
                values = [
                    candidate.get("display_name") or "",
                    candidate.get("name") or "",
                    *(candidate.get("raw_names", ()) or ()),
                ]
                if any(normalize_mod_text(value) == normalized or compact_mod_text(value) == compact for value in values):
                    exact_matches.append(candidate)
        if not exact_matches:
            return None
        entry = exact_matches[0]
        display_name = str(entry.get("display_name") or entry.get("name") or raw).strip() or raw
        workshop_id = str(entry.get("workshop_id") or "").strip()
        return {
            "display_name": display_name,
            "workshop_id": workshop_id,
            "query_term": workshop_id or display_name,
        }

    def _chip_from_suggestion(self, name: str, suggestion_entry: dict | None = None) -> dict:
        raw = str(name or "").strip()
        entry = suggestion_entry if isinstance(suggestion_entry, dict) else None
        if entry is None:
            resolved = self.resolve_exact_chip(raw)
            if resolved is not None:
                return resolved
            return {}
        display_name = str(entry.get("display_name") or entry.get("name") or raw).strip() or raw
        workshop_id = str(entry.get("workshop_id") or "").strip()
        return {
            "display_name": display_name,
            "workshop_id": workshop_id,
            "query_term": workshop_id or display_name,
        }

    def add_chip(self, chip: dict) -> bool:
        if not isinstance(chip, dict):
            return False
        display_name = str(chip.get("display_name") or chip.get("query_term") or "").strip()
        query_term = str(chip.get("query_term") or display_name).strip()
        if not display_name:
            return False
        if not query_term:
            return False
        normalized_chip = {
            "display_name": display_name or query_term,
            "workshop_id": str(chip.get("workshop_id") or "").strip(),
            "query_term": query_term,
        }
        new_key = self._chip_key(normalized_chip)
        chips = []
        replaced = False
        for existing in self.selected_chips or []:
            if not isinstance(existing, dict):
                continue
            if self._chip_key(existing) == new_key:
                chips.append(normalized_chip)
                replaced = True
            else:
                chips.append(existing)
        if not replaced:
            chips.append(normalized_chip)
        self.selected_chips = chips
        self._refresh_chip_ui()
        self._cancel_search_filter_debounce()
        self._apply_search_filter(scroll=True, reason="search")
        return True

    def remove_chip(self, chip_index: int) -> bool:
        try:
            index = int(chip_index)
        except Exception:
            return False
        chips = [chip for chip in (self.selected_chips or []) if isinstance(chip, dict)]
        if index < 0 or index >= len(chips):
            return False
        del chips[index]
        self.selected_chips = chips
        self._refresh_chip_ui()
        self._cancel_search_filter_debounce()
        self._apply_search_filter(scroll=True, reason="search")
        return True

    def _commit_input_as_chip(self) -> bool:
        entry = self._entry
        if not entry or not bool(self.mode_active):
            return False
        text = (entry.get_text() or "").strip()
        if not text:
            return False
        chip = self.resolve_exact_chip(text)
        if chip is None:
            try:
                self.dismissed = None
                self.refresh()
            except Exception:
                pass
            return True
        if not self.add_chip(chip):
            return False
        self.entry_update_guard = True
        try:
            entry.set_text("")
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
        finally:
            self.entry_update_guard = False
        try:
            self.cancel_refresh()
            self.hide("mod_search_commit")
        except Exception:
            pass
        self.restore_entry_cursor(0)
        GLib.idle_add(self.restore_entry_cursor, 0)
        return True

    def exit_mod_search_mode(self) -> bool:
        if not bool(self.mode_active):
            return False

        entry = self._entry
        saved_text = str(self.saved_normal_text or "")
        self.mode_active = False
        self.entry_update_guard = True
        try:
            if entry:
                entry.set_text(saved_text)
                entry.set_placeholder_text("Filter by name or IP. Click MOD for required mods.")
                icon = "edit-clear-symbolic" if saved_text else None
                entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, icon)
                try:
                    entry.remove_css_class("mod-search-entry-active")
                except Exception:
                    pass
        finally:
            self.entry_update_guard = False

        sync_mod_search_ui = self._sync_mode_ui
        if callable(sync_mod_search_ui):
            sync_mod_search_ui(False)

        try:
            self.cancel_refresh()
            self.hide("mod_search_escape")
        except Exception:
            pass
        if entry:
            cursor_pos = len(saved_text)
            self.restore_entry_cursor(cursor_pos)
            GLib.idle_add(self.restore_entry_cursor, cursor_pos)
        try:
            self._cancel_search_filter_debounce()
            self._apply_search_filter(scroll=True, reason="search")
        except Exception:
            pass
        return True

    def reset(self):
        """Leave Mod Search and drop every chip, for the browser's filter reset."""
        self.mode_active = False
        self.saved_normal_text = ""
        self.selected_chips = []
        sync_mod_search_ui = self._sync_mode_ui
        if callable(sync_mod_search_ui):
            sync_mod_search_ui(False)
        self._refresh_chip_ui()
        try:
            self.cancel_refresh()
            self.hide("reset")
        except Exception:
            pass

    # ------------------------------------------------------------- popover rows

    def log(self, message: str):
        if not self._debug_enabled():
            return
        try:
            print(f"[MODSUGGEST] {message}", flush=True)
        except Exception:
            pass

    def cancel_refresh(self):
        tid = int(self.refresh_source_id or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self.refresh_source_id = 0

    def queue_refresh(self):
        entry = self._entry
        if not entry:
            return
        self.cancel_refresh()
        text = entry.get_text() or ""

        def run_refresh():
            self.refresh_source_id = 0
            current_entry = self._entry
            if not current_entry:
                return False
            if (current_entry.get_text() or "") != text:
                return False
            self.refresh()
            return False

        self.refresh_source_id = GLib.timeout_add(SUGGESTION_REFRESH_DEBOUNCE_MS, run_refresh)

    def forget_rows(self):
        """The catalogue index was replaced: the next refresh must rebuild the rows."""
        self.last_rows = ()

    def _clear_rows(self):
        suggestion_list = self._list
        if not suggestion_list:
            return
        child = suggestion_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            suggestion_list.remove(child)
            child = next_child

    def hide(self, reason: str = "other"):
        panel = self._panel
        visible_before = False
        if panel:
            try:
                visible_before = bool(panel.get_visible())
            except Exception:
                visible_before = False
            panel.set_visible(False)
        self._clear_rows()
        self.last_rows = ()
        if visible_before or reason not in ("ineligible_token", "no_operator", "empty_token"):
            self.log(f"hide reason={reason} visible_before={visible_before}")
        return False

    def is_visible(self) -> bool:
        try:
            return bool(self._panel.get_visible())
        except Exception:
            return False

    def restore_entry_cursor(self, cursor_pos: int):
        entry = self._entry
        if not entry:
            return False
        try:
            entry.grab_focus()
        except Exception:
            pass
        try:
            entry.set_position(int(cursor_pos))
        except Exception:
            pass
        try:
            entry.select_region(int(cursor_pos), int(cursor_pos))
        except Exception:
            pass
        return False

    def _row_count(self) -> int:
        suggestion_list = self._list
        if not suggestion_list:
            return 0
        count = 0
        child = suggestion_list.get_first_child()
        while child is not None:
            count += 1
            child = child.get_next_sibling()
        return count

    def _scroll_row_into_view(self, row):
        if row is None:
            return False
        try:
            row.grab_focus()
        except Exception:
            pass
        scroller = self._scroller
        if not scroller:
            return False
        try:
            vadjustment = scroller.get_vadjustment()
        except Exception:
            vadjustment = None
        if vadjustment is None:
            return False
        try:
            row_top = float(row.get_allocation().y)
            row_bottom = row_top + float(row.get_allocated_height())
            visible_top = float(vadjustment.get_value())
            visible_bottom = visible_top + float(vadjustment.get_page_size())
            upper = float(vadjustment.get_upper())
            page_size = float(vadjustment.get_page_size())
        except Exception:
            return False
        new_value = visible_top
        if row_top < visible_top:
            new_value = row_top
        elif row_bottom > visible_bottom:
            new_value = row_bottom - page_size
        new_value = max(0.0, min(new_value, max(0.0, upper - page_size)))
        if new_value != visible_top:
            try:
                vadjustment.set_value(new_value)
            except Exception:
                pass
        return False

    def _select_delta(self, delta: int):
        suggestion_list = self._list
        if not suggestion_list:
            return
        count = self._row_count()
        if count <= 0:
            return
        selected = suggestion_list.get_selected_row()
        if selected is None:
            index = 0 if delta >= 0 else count - 1
        else:
            index = max(0, min(count - 1, selected.get_index() + delta))
        row = suggestion_list.get_row_at_index(index)
        if row is not None:
            suggestion_list.select_row(row)
            self._scroll_row_into_view(row)
            GLib.idle_add(self._scroll_row_into_view, row)

    def _activate_selected(self) -> bool:
        suggestion_list = self._list
        if not suggestion_list:
            return False
        row = suggestion_list.get_selected_row()
        if row is None:
            row = suggestion_list.get_row_at_index(0)
        if row is None:
            return False
        name = str(getattr(row, "_mod_suggestion_name", "") or "").strip()
        if not name:
            return False
        self.apply_suggestion(name)
        return True

    def _dismiss_key(self, text: str, start: int, compact_token: str) -> tuple[str, int, str]:
        return (str(text or "")[:start], int(start), str(compact_token or ""))

    def current_token(self):
        entry = self._entry
        if not entry:
            return None
        text = entry.get_text() or ""
        cursor = entry.get_position()
        if bool(self.mode_active):
            try:
                cursor = int(cursor)
            except Exception:
                cursor = len(text)
            cursor = max(0, min(cursor, len(text)))
            start = 0
            end = len(text)
            token = text.strip()
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            compact_token = compact_mod_text(token)
            if not compact_token:
                return None
            if len(compact_token) < 2 and compact_token not in SUGGESTION_SHORTHANDS:
                return None
            return token, start, end, compact_token

        normal_query, raw_mod_query, mod_query_mode = split_mod_search_operator(text)
        if not mod_query_mode:
            return None
        mod_start = len(text) - len(raw_mod_query)
        if cursor < mod_start:
            return None
        token_bounds = current_comma_token(raw_mod_query, cursor - mod_start)
        token, start, end = token_bounds
        start += mod_start
        end += mod_start
        compact_token = compact_mod_text(token)
        if not compact_token:
            return None
        if len(compact_token) < 2 and compact_token not in SUGGESTION_SHORTHANDS:
            return None
        return token, start, end, compact_token

    def refresh(self):
        panel = self._panel
        suggestion_list = self._list
        entry = self._entry
        if not panel or not suggestion_list or not entry:
            self.log(
                f"refresh abort missing_widgets panel={bool(panel)} list={bool(suggestion_list)} entry={bool(entry)}"
            )
            return
        if bool(self.selecting):
            return

        token_info = self.current_token()
        if token_info is None:
            self.hide("ineligible_token")
            return
        token, start, end, compact_token = token_info
        text = entry.get_text() or ""
        dismissed = self._dismiss_key(text, start, compact_token)
        if dismissed == self.dismissed:
            self.hide("dismissed_token")
            return

        catalogue = self._catalogue
        if (not bool(catalogue.ready) and
                (bool(catalogue.restore_pending) or bool(catalogue.restore_inflight))):
            self.log("catalogue not ready; suggestions temporarily unavailable")
            self.hide("catalogue_not_ready")
            return

        try:
            if (not bool(catalogue.ready) and
                    len(catalogue.index or {}) <= 0 and self._has_rows()):
                self.log("index empty; rebuilding from loaded rows")
                catalogue.rebuild_index()
        except Exception:
            pass
        index = catalogue.index
        try:
            index_count = len(index) if index is not None else -1
        except Exception:
            index_count = -1
        suggestions = suggest_mods(catalogue.index, token, limit=MOD_SUGGESTION_RESULT_LIMIT)
        if not suggestions:
            self.log(f"no suggestions token={token!r} index_count={index_count}")
            self.hide("no_suggestions")
            return
        first_names = [str(suggestion.get("display_name") or suggestion.get("name") or "") for suggestion in suggestions[:3]]
        row_entries = tuple(
            (str(suggestion.get("display_name") or suggestion.get("name") or "").strip(), suggestion)
            for suggestion in suggestions
        )
        row_entries = tuple((name, suggestion) for name, suggestion in row_entries if name)
        row_names = tuple(name for name, _suggestion in row_entries)
        visible_before = bool(panel.get_visible())
        if row_names == self.last_rows:
            if suggestion_list.get_first_child() is None:
                self.last_rows = ()
            else:
                if suggestion_list.get_selected_row() is None:
                    first_row = suggestion_list.get_row_at_index(0)
                    if first_row is not None:
                        suggestion_list.select_row(first_row)
                panel.set_visible(True)
                self.log(
                    f"show token={token!r} count={len(row_names)} first={first_names!r} rows=unchanged visible_before={visible_before} visible_after={panel.get_visible()} "
                    f"panel_parent={bool(panel.get_parent())} panel_mapped={panel.get_mapped()} list_rows={self._row_count()}"
                )
                return

        self._clear_rows()

        for name, suggestion in row_entries:
            label = Gtk.Label(label=name, xalign=0.0)
            label.set_can_focus(False)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_ellipsize(Pango.EllipsizeMode.END)

            row = Gtk.ListBoxRow()
            row.set_can_focus(False)
            row._mod_suggestion_name = name
            row._mod_suggestion_entry = suggestion
            row.set_child(label)
            suggestion_list.append(row)

        if suggestion_list.get_first_child() is None:
            self.hide("empty_rows_after_build")
            return

        first_row = suggestion_list.get_row_at_index(0)
        if first_row is not None:
            suggestion_list.select_row(first_row)
        self.last_rows = row_names
        panel.set_visible(True)
        self.log(
            f"show token={token!r} count={len(row_names)} first={first_names!r} rows=rebuilt visible_before={visible_before} visible_after={panel.get_visible()} "
            f"panel_parent={bool(panel.get_parent())} panel_mapped={panel.get_mapped()} list_rows={self._row_count()}"
        )

    def apply_suggestion(self, name: str):
        entry = self._entry
        if not entry:
            return

        self.selecting = True
        try:
            text = entry.get_text() or ""
            cursor = entry.get_position()
            if bool(self.mode_active):
                suggestion_entry = None
                suggestion_list = self._list
                row = suggestion_list.get_selected_row() if suggestion_list else None
                if row is not None and str(getattr(row, "_mod_suggestion_name", "") or "") == name:
                    suggestion_entry = getattr(row, "_mod_suggestion_entry", None)
                chip = self._chip_from_suggestion(name, suggestion_entry)
                if self.add_chip(chip):
                    self.entry_update_guard = True
                    try:
                        entry.set_text("")
                        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
                    finally:
                        self.entry_update_guard = False
                    self.restore_entry_cursor(0)
                    GLib.idle_add(self.restore_entry_cursor, 0)
            else:
                _, raw_mod_query, mod_query_mode = split_mod_search_operator(text)
                if not mod_query_mode:
                    return
                mod_start = len(text) - len(raw_mod_query)
                if cursor < mod_start:
                    return
                token, token_start, token_end = current_comma_token(raw_mod_query, cursor - mod_start)
                append_next_separator = not raw_mod_query[token_end:].strip()
                new_mod_query, mod_cursor = replace_comma_token(raw_mod_query, cursor - mod_start, name)
                if append_next_separator:
                    new_mod_query = f"{new_mod_query}, "
                    mod_cursor += 2
                new_text = f"{text[:mod_start]}{new_mod_query}"
                new_cursor = mod_start + mod_cursor
                entry.set_text(new_text)
                self.restore_entry_cursor(new_cursor)
                GLib.idle_add(self.restore_entry_cursor, new_cursor)
                _, new_raw_mod_query, new_mod_query_mode = split_mod_search_operator(new_text)
                if new_mod_query_mode:
                    new_mod_start = len(new_text) - len(new_raw_mod_query)
                    next_token, start, _end = current_comma_token(new_raw_mod_query, new_cursor - new_mod_start)
                    start += new_mod_start
                    self.dismissed = self._dismiss_key(
                        new_text,
                        start,
                        compact_mod_text(next_token),
                    )
        finally:
            self.selecting = False
        self.hide("selection")

    # ------------------------------------------------------------- GTK signal handlers

    def on_row_activated(self, _listbox, row):
        if not row:
            return
        name = str(getattr(row, "_mod_suggestion_name", "") or "").strip()
        if name and bool(self.mode_active):
            chip = self._chip_from_suggestion(name, getattr(row, "_mod_suggestion_entry", None))
            if self.add_chip(chip):
                entry = self._entry
                if entry:
                    self.entry_update_guard = True
                    try:
                        entry.set_text("")
                        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
                    finally:
                        self.entry_update_guard = False
                    self.restore_entry_cursor(0)
                    GLib.idle_add(self.restore_entry_cursor, 0)
                self.hide("selection")
        elif name:
            self.apply_suggestion(name)

    def on_key_pressed(self, _controller, keyval, _keycode, _state):
        visible = self.is_visible()
        if bool(self.mode_active) and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if visible:
                self._activate_selected()
                return True
            return self._commit_input_as_chip()
        if visible and keyval == Gdk.KEY_Down:
            self._select_delta(1)
            return True
        if visible and keyval == Gdk.KEY_Up:
            self._select_delta(-1)
            return True
        if visible and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._activate_selected()
            return True
        if visible and keyval == Gdk.KEY_Escape:
            token_info = self.current_token()
            if token_info is not None:
                _, start, end, compact_token = token_info
                text = self._entry.get_text() or ""
                self.dismissed = self._dismiss_key(text, start, compact_token)
            self.hide("escape")
            return True
        return False
