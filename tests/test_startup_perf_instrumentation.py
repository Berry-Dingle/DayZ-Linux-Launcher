"""Offline checks that startup timing remains observational."""

import time
from types import SimpleNamespace

import pytest

from dzll_launcher import mod_suggestion_cache, mod_suggestions, startup_perf, window
from dzll_launcher.window import DZLLWindow


class TinyStore:
    def __init__(self):
        self.rows = []

    def get_n_items(self):
        return len(self.rows)

    def append(self, row):
        self.rows.append(row)

    def get_item(self, index):
        return self.rows[index]

    def splice(self, index, count, replacement):
        self.rows[index:index + count] = list(replacement)


def test_phase_line_has_monotonic_duration_cumulative_time_and_thread(capsys):
    origin = time.monotonic_ns() - 10_000_000
    startup_perf.log_startup_phase(origin, "sample_phase", origin + 2_000_000,
                                   duration_ns=3_000_000, rows=12)
    line = capsys.readouterr().out.strip()
    assert line.startswith("[STARTUP-PERF] phase=sample_phase duration_ms=3.000")
    assert "startup_ms=" in line
    assert "thread=" in line
    assert "rows=12" in line


@pytest.mark.parametrize("enabled", [False, True])
def test_cache_load_stages_join_existing_phase_only_when_enabled(tmp_path, monkeypatch, enabled):
    path = tmp_path / "mod_suggestions_v1.json"
    index = mod_suggestions.build_mod_suggestion_index(('[{"steamWorkshopId":"42","name":"Example"}]',))
    mod_suggestion_cache.save_suggestion_cache(path, index)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", enabled)
    phases = []
    monkeypatch.setattr(window, "log_startup_phase",
                        lambda _origin, phase, _start, **fields: phases.append((phase, fields)))
    host = SimpleNamespace(
        _suggestion_cache_path=path,
        _startup_perf_origin_ns=time.monotonic_ns(),
        _startup_perf_db_apply_context={"load_id": 9, "input_rows": 12},
        _obj_by_key={},
        _mod_suggestion_last_rows=("stale",),
    )
    DZLLWindow._rebuild_mod_suggestion_index(host)
    assert host._mod_suggestion_index == index
    assert host._mod_suggestion_last_rows == ()
    if enabled:
        assert len(phases) == 1 and phases[0][0] == "mod_suggestion_cache_load"
        fields = phases[0][1]
        assert fields["result"] == "hit" and fields["bytes_read"] == path.stat().st_size
        assert fields["format"] == "v2"
        assert fields["load_id"] == 9 and fields["timer_scope"] == "nested"
        for name in ("file_stat_read", "json_decode", "canonical_json", "sha256",
                     "integrity", "index_restore", "entry_restore", "prefix_restore", "exact_restore", "total"):
            assert float(fields[f"{name}_ms"]) >= 0
    else:
        assert phases == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("fetched_ok", [False, True])
def test_db_apply_instrumentation_preserves_cached_and_fetched_flow(monkeypatch, enabled, fetched_ok):
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", enabled)
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda *_: 1)
    phases = []
    monkeypatch.setattr(window, "log_startup_phase",
                        lambda _origin, phase, _start, **fields: phases.append((phase, fields)))
    events = []
    host = SimpleNamespace(
        _startup_perf_origin_ns=time.monotonic_ns(),
        _startup_live_generation=0,
        _server_companion_rows_loaded=False,
        _obj_by_key={"10.0.0.1:2302": SimpleNamespace(bm_rank=50)},
        _set_map_choices=lambda _choices: events.append("maps"),
        _load_rows_into_store=lambda _rows: events.append("load") or True,
        _restore_server_companion_if_enabled=lambda: events.append("restore"),
        _set_updating=lambda *_: events.append("updating"),
        _auto_sort_lowest_ping_after_startup=lambda: events.append("sort"),
        _complete_startup_presentation=lambda: events.append("reveal"),
        _submit_startup_rest_batches=lambda *_args, **_kw: events.append("warmup"),
        _bm_live_group=lambda _key: 0,
        _apply_titlebar_counts=lambda: events.append("title"),
        _offline_recheck_tick=lambda: False,
        empty_label=SimpleNamespace(set_text=lambda *_: None, set_visible=lambda *_: None),
    )
    rows = [{"ip": "10.0.0.1", "gport": 2302, "qport": 2303, "map": "Chernarus"}]
    DZLLWindow._apply_db_rows(host, rows, fetched_ok)
    expected = ["maps", "load", "restore", "updating"]
    expected += ["warmup", "title"] if fetched_ok else ["sort", "reveal"]
    assert events == expected
    expected_phases = [
        "endpoint_validation", "map_choice_build", "map_model_update", "map_preparation",
        "startup_row_load_path",
    ]
    expected_phases += ["a2s_initial_submission", "post_load_startup_coordination"] if fetched_ok else [
        "post_load_startup_coordination"
    ]
    assert [phase for phase, _fields in phases] == (expected_phases if enabled else [])
    if enabled:
        assert {fields["load_id"] for _phase, fields in phases} == {1}
        assert {fields["input_rows"] for _phase, fields in phases} == {1}
        assert {fields["valid_rows"] for _phase, fields in phases} == {1}
        assert {fields["server_cache_state"] for _phase, fields in phases} == {"disk_load"}
        assert {fields["suggestion_cache_state"] for _phase, fields in phases} == {"disk_lookup_pending"}
        scopes = {phase: fields["timer_scope"] for phase, fields in phases}
        assert scopes["map_preparation"] == "parent"
        assert scopes["map_choice_build"] == scopes["map_model_update"] == "exclusive"


@pytest.mark.parametrize("enabled", [False, True])
def test_warmup_instrumentation_preserves_flush_sort_reveal_order(monkeypatch, enabled):
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", enabled)
    phases = []
    monkeypatch.setattr(window, "log_startup_phase",
                        lambda _origin, phase, _start, **_fields: phases.append(phase))
    events = []
    host = SimpleNamespace(
        _startup_perf_origin_ns=time.monotonic_ns(),
        _startup_perf_db_apply_context={
            "load_id": 7, "input_rows": 1, "valid_rows": 1,
            "server_cache_state": "disk_load", "suggestion_cache_state": "disk_lookup_pending",
        },
        _startup_perf_warmup_start_ns=time.monotonic_ns(),
        _startup_live_generation=1,
        _startup_warmup_revealed=False,
        _startup_warmup_timeout_id=0,
        _startup_live_rest_flush_id=0,
        _startup_warmup_completed=2,
        _startup_warmup_target=2,
        _flush_startup_rest_results=lambda _generation: events.append("flush"),
        _auto_sort_lowest_ping_after_startup=lambda: events.append("sort"),
        _complete_startup_presentation=lambda: events.append("reveal"),
        _set_updating=lambda *_: events.append("updating"),
        _startup_presentation=SimpleNamespace(startup_completed=False),
    )
    DZLLWindow._complete_startup_warmup(host, 1)
    DZLLWindow._complete_startup_warmup(host, 1)
    assert events == ["flush", "sort", "reveal"]
    assert phases == (["a2s_warmup_threshold", "a2s_warmup_reveal"] if enabled else [])


def test_db_apply_load_id_increments_for_replacement_load(monkeypatch):
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    phases = []
    monkeypatch.setattr(window, "log_startup_phase",
                        lambda _origin, phase, _start, **fields: phases.append((phase, fields)))
    host = SimpleNamespace(
        _startup_perf_origin_ns=time.monotonic_ns(),
        _set_map_choices=lambda _choices: None,
        _load_rows_into_store=lambda _rows: False,
        _complete_startup_presentation=lambda: None,
        empty_label=SimpleNamespace(set_text=lambda *_: None, set_visible=lambda *_: None),
    )
    row = {"ip": "10.0.0.1", "gport": 2302, "qport": 2303, "map": "Chernarus"}
    DZLLWindow._apply_db_rows(host, [row], False)
    DZLLWindow._apply_db_rows(host, [row], True)
    endpoint_load_ids = [fields["load_id"] for phase, fields in phases if phase == "endpoint_validation"]
    assert endpoint_load_ids == [1, 2]


@pytest.mark.parametrize("enabled", [False, True])
def test_row_phase_instrumentation_keeps_same_source_rows(monkeypatch, enabled):
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", enabled)
    phases = []
    monkeypatch.setattr(window, "log_startup_phase",
                        lambda _origin, phase, _start, **fields: phases.append((phase, fields)))
    events = []
    host = SimpleNamespace(
        _startup_perf_origin_ns=time.monotonic_ns(),
        _startup_perf_db_apply_context={
            "load_id": 7, "input_rows": 1, "valid_rows": 1,
            "server_cache_state": "disk_load", "suggestion_cache_state": "disk_lookup_pending",
        },
        store=TinyStore(), _obj_by_key={}, _browser_live_offline_streaks={},
        live={}, favorites={}, last_played={},
        _is_likely_test_server_name=lambda *_: False,
        _debug_sort_attach_notify_probe=lambda *_: None,
        _rebuild_mod_suggestion_index=lambda: events.append("mod_index"),
        _snapshot_all_sort_keys=lambda: events.append("snapshot"),
        _on_filter_changed=lambda **_kw: events.append("filter"),
        _apply_titlebar_counts=lambda: events.append("title"),
    )
    row = {"ip": "10.0.0.1", "gport": 2302, "qport": 2303, "name": "One",
           "map": "Chernarus", "mods": "[]", "players": 2, "ping": 50}
    assert DZLLWindow._load_rows_into_store(host, [row]) is True
    assert [(obj.ip, obj.gport) for obj in host.store.rows] == [("10.0.0.1", 2302)]
    assert events == ["mod_index", "snapshot", "filter", "title"]
    expected_phases = [
        "source_store_clear", "source_sidecar_clear", "server_mod_cache_apply",
        "row_scalar_conversion", "row_sidecar_attachment", "row_conversion", "mod_preview_index",
        "server_object_construction", "source_store_insertion", "row_loop_total", "global_mod_suggestion_index",
        "sort_key_snapshot",
    ]
    assert [phase for phase, _fields in phases] == (expected_phases if enabled else [])
    if enabled:
        assert {fields["load_id"] for _phase, fields in phases} == {7}
        scopes = {phase: fields["timer_scope"] for phase, fields in phases}
        assert scopes["server_mod_cache_apply"] == "overlap"
        assert scopes["row_loop_total"] == "parent"
        assert scopes["row_conversion"] == "exclusive_residual"
        assert scopes["row_scalar_conversion"] == scopes["row_sidecar_attachment"] == "exclusive_aggregate"


@pytest.mark.parametrize("enabled", [False, True])
def test_initial_rebuild_instrumentation_preserves_filter_and_sort(monkeypatch, enabled):
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", enabled)
    phases = []
    monkeypatch.setattr(window, "log_startup_phase",
                        lambda _origin, phase, _start, **_fields: phases.append(phase))
    source = TinyStore()
    visible = TinyStore()
    for name, ping in (("Slow", 80), ("Fast", 20)):
        source.append(window.ServerObject(name=name, ip=f"10.0.0.{ping}",
                                          gport=2302, qport=2303, ping=ping))
    host = SimpleNamespace(
        _startup_perf_origin_ns=time.monotonic_ns(),
        _startup_perf_db_apply_context={
            "load_id": 3, "input_rows": 2, "valid_rows": 2,
            "server_cache_state": "disk_load", "suggestion_cache_state": "disk_hit",
        },
        _startup_presentation=SimpleNamespace(startup_completed=False),
        _startup_live_rest_started_at=0.0,
        store=source, column_view_store=visible,
        sort_key="ping", sort_asc=True, settings={},
        _debug_sort_note_model_event=lambda *_: None,
        _debug_browser_reorder=lambda *_, **_kw: None,
        _browser_reorder_is_background_reason=lambda *_: False,
        _combined_filter_func=lambda obj: obj.name != "Slow",
    )
    DZLLWindow._rebuild_column_view_store(host, reorder_reason="load")
    assert [obj.name for obj in visible.rows] == ["Fast"]
    assert phases == ([
        "initial_filter_scan", "initial_sort", "initial_visible_store_splice",
        "initial_visible_store_rebuild",
    ] if enabled else [])


def test_next_frame_tick_is_labeled_as_latency_marker_not_draw_completion(monkeypatch):
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    phases = []
    monkeypatch.setattr(window, "log_startup_phase",
                        lambda _origin, phase, _start, **fields: phases.append((phase, fields)))
    callbacks = []
    widget = SimpleNamespace(add_tick_callback=lambda callback: callbacks.append(callback) or 41)
    host = SimpleNamespace(
        _startup_perf_origin_ns=time.monotonic_ns(),
        _startup_perf_db_apply_context={
            "load_id": 5, "input_rows": 12, "valid_rows": 12,
            "server_cache_state": "disk_load", "suggestion_cache_state": "disk_hit",
        },
        list_view=widget,
    )
    window._schedule_startup_perf_next_frame_tick(host, 10)
    assert len(callbacks) == 1
    host._startup_perf_db_apply_context["load_id"] = 6
    assert callbacks[0](widget, None) is False
    assert phases == [("initial_next_frame_tick", {
        "timer_scope": "latency_marker",
        "meaning": "next_frame_clock_tick_not_draw_complete",
        "visible_rows": 10,
        "load_id": 5,
        "input_rows": 12,
        "valid_rows": 12,
        "server_cache_state": "disk_load",
        "suggestion_cache_state": "disk_hit",
    })]
