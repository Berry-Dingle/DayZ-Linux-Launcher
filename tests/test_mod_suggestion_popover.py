"""Tests for ModSuggestionPopover: popover lifecycle, keyboard navigation, chips, debounce and cancellation."""

from types import SimpleNamespace

import pytest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from dzll_launcher import mod_suggestion_popover as module, mod_suggestions, sidebar_ui, window  # noqa: E402
from dzll_launcher.mod_suggestion_popover import ModSuggestionPopover  # noqa: E402
from dzll_launcher.window import DZLLWindow  # noqa: E402

PAYLOADS = (
    '[{"steamWorkshopId":"42","name":"Code Lock"},'
    '{"steamWorkshopId":"77","name":"Community Framework"},'
    '{"steamWorkshopId":"88","name":"CF Tools"},'
    '{"name":"Base Building Plus"}]',
)
MANY_PAYLOADS = tuple(
    "[" + ",".join(
        f'{{"steamWorkshopId":"{1000 + n}","name":"Mod {n:02d} Pack"}}' for n in range(start, start + 10)
    ) + "]"
    for start in range(0, 30, 10)
)


class Scheduler:
    """Deterministic stand-in for the GLib calls the popover makes."""

    def __init__(self):
        self.next_id = 100
        self.sources = {}
        self.removed = []
        self.fail_remove = False

    def timeout_add(self, delay, fn, *args):
        self.next_id += 1
        self.sources[self.next_id] = ("timeout", delay, fn, args)
        return self.next_id

    def idle_add(self, fn, *args):
        self.next_id += 1
        self.sources[self.next_id] = ("idle", 0, fn, args)
        return self.next_id

    def source_remove(self, source_id):
        self.removed.append(source_id)
        self.sources.pop(source_id, None)
        if self.fail_remove:
            raise RuntimeError("already removed")
        return True

    def of_kind(self, kind):
        return [(delay, fn, args) for _sid, (k, delay, fn, args) in sorted(self.sources.items()) if k == kind]

    def fire(self, kind):
        results = []
        for source_id in sorted(self.sources):
            k, _delay, fn, args = self.sources[source_id]
            if k == kind:
                del self.sources[source_id]
                results.append(fn(*args))
        return results


@pytest.fixture
def sched(monkeypatch):
    scheduler = Scheduler()
    monkeypatch.setattr(module, "GLib", SimpleNamespace(
        timeout_add=scheduler.timeout_add,
        idle_add=scheduler.idle_add,
        source_remove=scheduler.source_remove,
    ))
    return scheduler


class FakeEntry:
    def __init__(self, text="", position=None, popover=None):
        self.text = text
        self.position = len(text) if position is None else position
        self.placeholder = None
        self.icon = "unset"
        self.css = {"mod-search-entry-active"}
        self.popover = popover
        self.set_text_calls = []
        self.focus_grabs = 0
        self.selected_region = None

    def get_text(self):
        return self.text

    def set_text(self, text):
        guard = self.popover.entry_update_guard if self.popover is not None else None
        self.set_text_calls.append((text, guard))
        self.text = text

    def get_position(self):
        return self.position

    def set_position(self, position):
        self.position = len(self.text) if position < 0 else position

    def select_region(self, start, end):
        self.selected_region = (start, end)

    def grab_focus(self):
        self.focus_grabs += 1

    def set_icon_from_icon_name(self, _position, icon):
        self.icon = icon

    def set_placeholder_text(self, text):
        self.placeholder = text

    def remove_css_class(self, name):
        self.css.discard(name)


def ready_catalogue(payloads=PAYLOADS, **state):
    values = dict(
        ready=True,
        restore_pending=False,
        restore_inflight=False,
        index=mod_suggestions.build_mod_suggestion_index(payloads),
        rebuild_index=lambda: None,
    )
    values.update(state)
    return SimpleNamespace(**values)


def make_popover(catalogue=None, *, events=None, debug=False, has_rows=None):
    events = events if events is not None else []
    popover = ModSuggestionPopover(
        catalogue=catalogue or ready_catalogue(),
        has_rows=has_rows or (lambda: {}),
        apply_search_filter=lambda **kwargs: events.append(("apply_search_filter", kwargs)),
        cancel_search_filter_debounce=lambda: events.append("cancel_search_filter_debounce"),
        debug_enabled=lambda: debug,
    )
    return popover, events


def bind_fakes(popover, entry, events, *, panel=None, suggestion_list=None):
    popover.bind_widgets(
        entry=entry,
        panel=panel,
        suggestion_list=suggestion_list,
        scroller=None,
        refresh_chips=lambda: events.append("refresh_chips"),
        sync_mode_ui=lambda active: events.append(("sync_mode_ui", active)),
    )


def require_display():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")


def bind_real(popover, events, text):
    entry = Gtk.Entry()
    entry.set_text(text)
    entry.set_position(-1)
    panel = Gtk.Box()
    panel.set_visible(False)
    scroller = Gtk.ScrolledWindow()
    suggestion_list = Gtk.ListBox()
    scroller.set_child(suggestion_list)
    panel.append(scroller)
    popover.bind_widgets(
        entry=entry, panel=panel, suggestion_list=suggestion_list, scroller=scroller,
        refresh_chips=lambda: events.append("refresh_chips"),
        sync_mode_ui=lambda active: events.append(("sync_mode_ui", active)),
    )
    return entry, panel, suggestion_list


def row_names(suggestion_list):
    names = []
    child = suggestion_list.get_first_child()
    while child is not None:
        names.append(child._mod_suggestion_name)
        child = child.get_next_sibling()
    return names


# ------------------------------------------------------------------ lifecycle


def test_initial_state_and_unbound_widgets_are_inert(sched, capsys):
    popover, events = make_popover(debug=True)
    assert (popover.selecting, popover.dismissed, popover.refresh_source_id, popover.last_rows) == (False, None, 0, ())
    assert (popover.mode_active, popover.saved_normal_text, popover.entry_update_guard) == (False, "", False)
    assert popover.selected_chips == []
    assert popover.normal_filter_text() == ""
    assert popover.current_token() is None
    assert popover.is_visible() is False
    assert popover.restore_entry_cursor(3) is False
    popover.queue_refresh()
    assert sched.sources == {}
    popover.refresh()
    assert capsys.readouterr().out == (
        "[MODSUGGEST] refresh abort missing_widgets panel=False list=False entry=False\n"
    )
    assert popover.hide("other") is False
    assert popover.exit_mod_search_mode() is False
    assert popover.on_key_pressed(None, Gdk.KEY_Down, 0, 0) is False
    assert events == []


def test_window_builder_wires_catalogue_filter_and_coordinator_callbacks(sched, monkeypatch):
    calls = []
    host = SimpleNamespace(
        _obj_by_key={"k": object()},
        _apply_search_filter_changed=lambda **kwargs: calls.append(("apply", kwargs)),
        _cancel_search_filter_debounce=lambda: calls.append("cancel"),
    )
    host._mod_suggestions = DZLLWindow._build_mod_suggestion_coordinator(host)
    host._mod_suggestion_popover = popover = DZLLWindow._build_mod_suggestion_popover(host)
    assert popover._catalogue is host._mod_suggestions
    assert popover._has_rows() is host._obj_by_key

    popover.add_chip({"display_name": "Code Lock", "workshop_id": "42"})
    assert calls == ["cancel", ("apply", {"scroll": True, "reason": "search"})]

    popover.last_rows = ("stale",)
    host._mod_suggestions._on_index_replaced()
    assert popover.last_rows == ()

    popover.bind_widgets(entry=FakeEntry("x -mods=co"), panel=None, suggestion_list=None,
                         scroller=None, refresh_chips=None, sync_mode_ui=None)
    host._mod_suggestions._on_restored()
    assert [delay for delay, _fn, _args in sched.of_kind("timeout")] == [35]

    monkeypatch.setattr(window, "DEBUG_MOD_SUGGESTIONS", True)
    assert popover._debug_enabled() is True
    monkeypatch.setattr(window, "DEBUG_MOD_SUGGESTIONS", False)
    assert popover._debug_enabled() is False


# ------------------------------------------------------------------ debounce and cancellation


def test_queue_refresh_debounces_35ms_and_replaces_the_pending_timer(sched):
    popover, events = make_popover()
    entry = FakeEntry("x -mods=co")
    bind_fakes(popover, entry, events)
    refreshes = []
    popover.refresh = lambda: refreshes.append(entry.get_text())

    popover.queue_refresh()
    first = popover.refresh_source_id
    popover.queue_refresh()
    second = popover.refresh_source_id

    assert module.SUGGESTION_REFRESH_DEBOUNCE_MS == 35
    assert first and second and first != second
    assert sched.removed == [first]
    assert [delay for delay, _fn, _args in sched.of_kind("timeout")] == [35]
    assert sched.fire("timeout") == [False]
    assert refreshes == ["x -mods=co"]
    assert popover.refresh_source_id == 0


def test_queued_refresh_is_dropped_when_the_text_changed_or_the_entry_is_gone(sched):
    popover, events = make_popover()
    entry = FakeEntry("x -mods=co")
    bind_fakes(popover, entry, events)
    refreshes = []
    popover.refresh = lambda: refreshes.append("refresh")

    popover.queue_refresh()
    entry.text = "x -mods=cod"
    assert sched.fire("timeout") == [False]
    assert popover.refresh_source_id == 0

    popover.queue_refresh()
    popover._entry = None
    assert sched.fire("timeout") == [False]
    assert refreshes == []
    assert popover.refresh_source_id == 0


def test_cancel_refresh_removes_the_timer_and_tolerates_a_removal_error(sched):
    popover, events = make_popover()
    bind_fakes(popover, FakeEntry("x -mods=co"), events)
    popover.cancel_refresh()
    assert sched.removed == []

    popover.queue_refresh()
    source_id = popover.refresh_source_id
    sched.fail_remove = True
    popover.cancel_refresh()
    assert sched.removed == [source_id]
    assert popover.refresh_source_id == 0
    assert sched.of_kind("timeout") == []


# ------------------------------------------------------------------ tokens and applying suggestions


def test_normal_mode_token_follows_the_cursor_and_rejects_short_terms(sched):
    popover, events = make_popover()
    entry = FakeEntry("x -mods=Code Lock, Da, Mod 1", position=12)
    bind_fakes(popover, entry, events)
    assert popover.current_token() == ("Code Lock", 8, 17, "codelock")
    entry.position = 21
    assert popover.current_token() == ("Da", 19, 21, "da")
    entry.text, entry.position = "x -mods=c", 9
    assert popover.current_token() is None
    entry.text, entry.position = "x -mods=cf", 10
    assert popover.current_token() == ("cf", 8, 10, "cf")
    entry.position = 2
    assert popover.current_token() is None
    entry.text, entry.position = "no operator", 11
    assert popover.current_token() is None


def test_mod_search_mode_token_is_the_trimmed_input(sched):
    popover, events = make_popover()
    entry = FakeEntry("  code lock  ", position=0)
    bind_fakes(popover, entry, events)
    popover.mode_active = True
    assert popover.current_token() == ("code lock", 2, 11, "codelock")
    entry.text = "  c "
    assert popover.current_token() is None


def test_normal_mode_apply_replaces_the_token_appends_a_separator_and_dismisses_the_next(sched):
    popover, events = make_popover()
    entry = FakeEntry("alpha -mods=co", popover=None)
    bind_fakes(popover, entry, events)
    hides = []
    popover.hide = lambda reason: hides.append((reason, popover.selecting))

    popover.apply_suggestion("Code Lock")

    assert entry.text == "alpha -mods=Code Lock, "
    assert entry.position == len("alpha -mods=Code Lock, ")
    assert entry.selected_region == (23, 23)
    assert [(fn.__name__, args) for _d, fn, args in sched.of_kind("idle")] == [("restore_entry_cursor", (23,))]
    assert popover.dismissed == ("alpha -mods=Code Lock, ", 23, "")
    assert hides == [("selection", False)]
    assert events == []


def test_normal_mode_apply_mid_list_keeps_following_terms(sched):
    popover, events = make_popover()
    entry = FakeEntry("x -mods=co, CF Tools", position=10)
    bind_fakes(popover, entry, events)
    popover.apply_suggestion("Code Lock")
    assert entry.text == "x -mods=Code Lock, CF Tools"
    assert entry.position == 17


# ------------------------------------------------------------------ chips


def test_add_replace_and_remove_chips_drive_the_search_filter(sched):
    popover, events = make_popover()
    bind_fakes(popover, FakeEntry(), events)

    assert popover.add_chip({"display_name": "Code Lock", "workshop_id": "42"}) is True
    assert popover.add_chip({"display_name": "Base Building Plus"}) is True
    assert popover.add_chip({"display_name": "Code Lock (renamed)", "workshop_id": " 42 ", "query_term": "42"}) is True
    assert popover.selected_chips == [
        {"display_name": "Code Lock (renamed)", "workshop_id": "42", "query_term": "42"},
        {"display_name": "Base Building Plus", "workshop_id": "", "query_term": "Base Building Plus"},
    ]
    assert popover.chip_query_terms() == ("42", "Base Building Plus")
    one_add = ["refresh_chips", "cancel_search_filter_debounce",
               ("apply_search_filter", {"scroll": True, "reason": "search"})]
    assert events == one_add * 3

    events.clear()
    assert popover.add_chip({}) is False
    assert popover.add_chip("Code Lock") is False
    assert popover.remove_chip(5) is False
    assert popover.remove_chip("x") is False
    assert events == []

    assert popover.remove_chip(0) is True
    assert popover.chip_query_terms() == ("Base Building Plus",)
    assert events == one_add


def test_exact_chip_resolution_waits_for_the_catalogue_and_matches_names(sched):
    catalogue = ready_catalogue(ready=False, restore_pending=True)
    popover, _events = make_popover(catalogue)
    assert popover.resolve_exact_chip("Code Lock") is None
    catalogue.ready = True
    assert popover.resolve_exact_chip("  code   lock ") == {
        "display_name": "Code Lock", "workshop_id": "42", "query_term": "42",
    }
    assert popover.resolve_exact_chip("basebuildingplus") == {
        "display_name": "BaseBuildingPlus", "workshop_id": "", "query_term": "BaseBuildingPlus",
    }
    assert popover.resolve_exact_chip("cod") is None
    assert popover.resolve_exact_chip("") is None


def test_enter_in_mod_search_commits_an_exact_chip_and_clears_the_entry(sched):
    popover, events = make_popover()
    entry = FakeEntry("Code Lock", popover=None)
    entry.popover = popover
    bind_fakes(popover, entry, events)
    popover.mode_active = True
    hides = []
    popover.hide = lambda reason: hides.append(reason)

    assert popover.on_key_pressed(None, Gdk.KEY_Return, 0, 0) is True

    assert popover.chip_query_terms() == ("42",)
    assert entry.set_text_calls == [("", True)]
    assert popover.entry_update_guard is False
    assert entry.icon is None
    assert hides == ["mod_search_commit"]
    assert [(fn.__name__, args) for _d, fn, args in sched.of_kind("idle")] == [("restore_entry_cursor", (0,))]
    assert entry.position == 0


def test_enter_in_mod_search_with_unknown_text_reopens_suggestions_without_a_chip(sched):
    popover, events = make_popover()
    entry = FakeEntry("zzzz")
    bind_fakes(popover, entry, events)
    popover.mode_active = True
    popover.dismissed = ("", 0, "zzzz")
    refreshes = []
    popover.refresh = lambda: refreshes.append(popover.dismissed)

    assert popover.on_key_pressed(None, Gdk.KEY_KP_Enter, 0, 0) is True
    assert refreshes == [None]
    assert popover.selected_chips == []
    entry.text = "  "
    assert popover.on_key_pressed(None, Gdk.KEY_Return, 0, 0) is False


def test_exit_mod_search_restores_the_normal_text_and_filter(sched):
    popover, events = make_popover()
    entry = FakeEntry("partial")
    entry.popover = popover
    bind_fakes(popover, entry, events)
    popover.mode_active = True
    popover.saved_normal_text = "my server"
    hides = []
    popover.hide = lambda reason: hides.append(reason)

    assert popover.exit_mod_search_mode() is True

    assert popover.mode_active is False
    assert entry.set_text_calls == [("my server", True)]
    assert entry.placeholder == "Filter by name or IP. Click MOD for required mods."
    assert entry.icon == "edit-clear-symbolic"
    assert "mod-search-entry-active" not in entry.css
    assert hides == ["mod_search_escape"]
    assert events == [("sync_mode_ui", False), "cancel_search_filter_debounce",
                      ("apply_search_filter", {"scroll": True, "reason": "search"})]
    assert entry.position == 9
    assert [(fn.__name__, args) for _d, fn, args in sched.of_kind("idle")] == [("restore_entry_cursor", (9,))]


def test_reset_leaves_mod_search_and_drops_chips_in_order(sched):
    popover, events = make_popover()
    bind_fakes(popover, FakeEntry(), events)
    popover.mode_active = True
    popover.saved_normal_text = "abc"
    popover.selected_chips = [{"display_name": "Code Lock", "query_term": "42"}]
    popover.queue_refresh()
    source_id = popover.refresh_source_id
    popover.hide = lambda reason: events.append(("hide", reason))

    popover.reset()

    assert (popover.mode_active, popover.saved_normal_text, popover.selected_chips) == (False, "", [])
    assert events == [("sync_mode_ui", False), "refresh_chips", ("hide", "reset")]
    assert sched.removed == [source_id]
    assert popover.refresh_source_id == 0


def test_filter_state_reads_saved_text_and_chips_from_the_popover(sched):
    popover, events = make_popover()
    bind_fakes(popover, FakeEntry("partial mod input"), events)
    host = SimpleNamespace(settings={}, live={}, _mod_suggestion_popover=popover)
    state = DZLLWindow._build_filter_state(host)
    assert (state["raw_query"], state["mod_query_mode"]) == ("partial mod input", False)

    popover.mode_active = True
    popover.saved_normal_text = "Alpha"
    popover.selected_chips = [{"display_name": "Code Lock", "query_term": "42"}]
    state = DZLLWindow._build_filter_state(host)
    assert state["raw_query"] == "alpha"
    assert state["mod_query_mode"] is True
    assert state["search_key"].startswith("text:alpha|mod:")
    assert [term[0] for term in state["mod_query"]] == ["42"]


def test_hide_logs_only_visible_or_notable_hides(sched, capsys):
    popover, events = make_popover(debug=True)
    panel = SimpleNamespace(visible=False)
    panel.get_visible = lambda: panel.visible
    panel.set_visible = lambda value: setattr(panel, "visible", value)
    bind_fakes(popover, FakeEntry(), events, panel=panel)
    popover.last_rows = ("a",)
    popover.hide("ineligible_token")
    assert capsys.readouterr().out == ""
    assert popover.last_rows == ()
    panel.visible = True
    popover.hide("ineligible_token")
    popover.hide("escape")
    assert capsys.readouterr().out == (
        "[MODSUGGEST] hide reason=ineligible_token visible_before=True\n"
        "[MODSUGGEST] hide reason=escape visible_before=False\n"
    )


def test_catalogue_gating_hides_without_a_rebuild_and_rebuilds_an_empty_index(sched):
    rebuilds = []
    catalogue = ready_catalogue(ready=False, restore_pending=True)
    catalogue.rebuild_index = lambda: rebuilds.append("rebuild")
    rows = {}
    popover, events = make_popover(catalogue, has_rows=lambda: rows)
    bind_fakes(popover, FakeEntry("x -mods=co"), events, panel=SimpleNamespace(), suggestion_list=SimpleNamespace())
    hides = []
    popover.hide = lambda reason: hides.append(reason)

    popover.refresh()
    assert hides == ["catalogue_not_ready"]

    catalogue.restore_pending = False
    catalogue.index = {}
    popover.refresh()
    assert rebuilds == []
    rows["k"] = object()
    catalogue.index = {}  # suggest_mods caches lookups inside the index dict
    popover.refresh()
    assert rebuilds == ["rebuild"]
    assert hides == ["catalogue_not_ready", "no_suggestions", "no_suggestions"]


# ------------------------------------------------------------------ real GTK rows and keyboard navigation


def test_refresh_builds_rows_and_keyboard_navigation_moves_the_selection(sched):
    require_display()
    popover, events = make_popover(ready_catalogue(MANY_PAYLOADS))
    entry, panel, suggestion_list = bind_real(popover, events, "x -mods=mod")

    popover.refresh()
    expected = tuple(
        s["display_name"] for s in mod_suggestions.suggest_mods(
            popover._catalogue.index, "mod", limit=module.MOD_SUGGESTION_RESULT_LIMIT,
        )
    )
    assert panel.get_visible() is True
    assert tuple(row_names(suggestion_list)) == expected == popover.last_rows
    assert len(expected) == 30
    assert suggestion_list.get_selected_row().get_index() == 0

    assert popover.on_key_pressed(None, Gdk.KEY_Down, 0, 0) is True
    assert popover.on_key_pressed(None, Gdk.KEY_Down, 0, 0) is True
    assert popover.on_key_pressed(None, Gdk.KEY_Up, 0, 0) is True
    assert suggestion_list.get_selected_row().get_index() == 1
    assert [fn.__name__ for _d, fn, _a in sched.of_kind("idle")] == ["_scroll_row_into_view"] * 3

    suggestion_list.unselect_all()
    assert popover.on_key_pressed(None, Gdk.KEY_Up, 0, 0) is True
    assert suggestion_list.get_selected_row().get_index() == 29

    first_rows = [suggestion_list.get_row_at_index(i) for i in range(3)]
    popover.refresh()
    assert [suggestion_list.get_row_at_index(i) for i in range(3)] == first_rows

    assert popover.on_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True
    assert panel.get_visible() is False
    assert row_names(suggestion_list) == []
    assert popover.last_rows == ()
    assert popover.dismissed == ("x -mods=", 8, "mod")
    popover.refresh()
    assert panel.get_visible() is False
    assert popover.on_key_pressed(None, Gdk.KEY_Down, 0, 0) is False


def test_enter_applies_the_selected_row_and_row_activation_adds_a_chip_in_mod_search(sched):
    require_display()
    popover, events = make_popover()
    entry, panel, suggestion_list = bind_real(popover, events, "x -mods=co")
    popover.refresh()
    assert row_names(suggestion_list) == ["Code Lock", "Community Framework"]
    popover.on_key_pressed(None, Gdk.KEY_Down, 0, 0)
    assert popover.on_key_pressed(None, Gdk.KEY_Return, 0, 0) is True
    assert entry.get_text() == "x -mods=Community Framework, "
    assert panel.get_visible() is False

    popover.mode_active = True
    entry.set_text("cf")
    popover.refresh()
    assert row_names(suggestion_list) == ["Community Framework", "CF Tools"]
    popover.on_row_activated(suggestion_list, suggestion_list.get_row_at_index(1))
    assert popover.selected_chips == [{"display_name": "CF Tools", "workshop_id": "88", "query_term": "88"}]
    assert entry.get_text() == ""
    assert panel.get_visible() is False
    assert "refresh_chips" in events


def test_sidebar_search_area_routes_entry_signals_to_the_popover(monkeypatch, sched):
    require_display()
    calls = []
    host = SimpleNamespace(
        _main_overlay=Gtk.Overlay(),
        _apply_search_filter_changed=lambda **kwargs: calls.append(("apply", kwargs)),
        _cancel_search_filter_debounce=lambda: calls.append("cancel"),
        _on_search_changed_debounced=lambda: calls.append("debounced"),
    )
    host._mod_suggestions = ready_catalogue()
    host._mod_suggestion_popover = popover = DZLLWindow._build_mod_suggestion_popover(host)
    monkeypatch.setattr(sidebar_ui.GLib, "idle_add", lambda *_args: 0)
    sidebar_ui.build_search_area(host)

    assert popover._entry is host.search_entry
    assert popover._panel is host.mod_suggestion_panel
    assert popover._list is host.mod_suggestion_list
    assert popover._scroller is host.mod_suggestion_scroller
    for name in ("_sync_mod_search_mode_ui", "_refresh_mod_search_chips", "_mod_search_mode_active"):
        assert not hasattr(host, name)

    host.search_entry.set_text("abc mods: Code Lock, co")
    assert popover.mode_active is True
    assert popover.saved_normal_text == "abc"
    assert popover.chip_query_terms() == ("42",)
    assert host.search_entry.get_text() == "co"
    assert host.mod_search_toggle_badge.get_active() is True
    assert host._mod_search_chip_terms == ("Code Lock",)

    host.search_entry.set_text("com")
    expected = [s["display_name"] for s in mod_suggestions.suggest_mods(host._mod_suggestions.index, "com", limit=50)]
    assert host.mod_suggestion_panel.get_visible() is True
    assert row_names(host.mod_suggestion_list) == expected
    assert expected[0] == "Community Framework"

    popover.exit_mod_search_mode()
    assert host.search_entry.get_text() == "abc"
    assert host.mod_search_toggle_badge.get_active() is False
