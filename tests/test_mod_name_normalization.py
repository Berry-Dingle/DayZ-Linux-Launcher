from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from dzll_launcher import mod_metadata, mod_name_resolver, steamcmd_mods
from dzll_launcher.column_view import _required_mod_names_from_json
from dzll_launcher.mod_metadata import (
    MOD_DISPLAY_NAME_MAX_CHARS,
    clean_display_mod_name,
    normalize_mod_display_name,
)
from dzll_launcher.mod_search import build_server_mod_index
from dzll_launcher.mod_suggestions import build_mod_suggestion_index
from dzll_launcher.mods_ui import ModsManagerOverlay
from dzll_launcher.preparation_contracts import (
    JoinPopupPreparationPresenter,
    PreparationOutcome,
    PreparationProgressEvent,
    PreparationStatus,
)
from dzll_launcher.preparation_presentation import PreparationPresentationReducer
from dzll_launcher.steamcmd_mods import (
    ensure_watch_symlinks,
    parse_mods_from_db,
    symlink_name_for_mod,
)
from dzll_launcher.window import parse_mods_preview


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LineA\nLineB", "LineA LineB"),
        ("LineA\rLineB", "LineA LineB"),
        ("TabA\tTabB", "TabA TabB"),
        ("  repeated\t\n spaces  ", "repeated spaces"),
        ("Nul\x00Tail", "NulTail"),
        ("C1\x80Tail", "C1Tail"),
        ("C1\x85Space", "C1 Space"),
        ("Innocent Mod \u202e ppa.exe", "Innocent Mod ppa.exe"),
        ("A \u2066RTL\u2069 Z", "A RTL Z"),
        ("Zero\u200bWidth", "ZeroWidth"),
        ("Word\u2060Joiner", "WordJoiner"),
        ("Stray\ufeffBOM", "StrayBOM"),
    ],
)
def test_shared_normalizer_rejects_control_and_format_characters(raw, expected):
    assert normalize_mod_display_name(raw) == expected


@pytest.mark.parametrize(
    "name",
    [
        "Café 日本語",
        "Український мод",
        "العربية",
        "क्\u200dष Mod",
        "فارسی\u200cنام",
        "👩\u200d💻 Clan Pack",
        "e\u0301 combining",
        "[PvE] C++ / #1 ± ∑ ♥",
    ],
)
def test_shared_normalizer_preserves_legitimate_unicode(name):
    assert normalize_mod_display_name(name) == name


def test_display_length_ceiling_is_code_point_based_and_deterministic():
    exact = "A" * MOD_DISPLAY_NAME_MAX_CHARS
    over = exact + "ignored"
    multibyte = "界" * (MOD_DISPLAY_NAME_MAX_CHARS + 20)

    assert normalize_mod_display_name("Short Name") == "Short Name"
    assert normalize_mod_display_name(exact) == exact
    assert normalize_mod_display_name(over) == exact
    assert normalize_mod_display_name(over) == normalize_mod_display_name(over)
    assert normalize_mod_display_name(multibyte) == "界" * MOD_DISPLAY_NAME_MAX_CHARS


def test_normalization_precedes_existing_workshop_id_suffix_cleanup():
    assert clean_display_mod_name("Watch\nName__1234", 1234) == "Watch Name"
    assert clean_display_mod_name("\x00\u202e", 1234) == "@Mod-ID - 1234"


@pytest.mark.parametrize(
    ("raw", "mod_id", "expected"),
    [
        ("Name__123", 123, "Name"),
        ("Name__123__456", 456, "Name__123"),
        ("Name__123__456", 123, "Name__123__456"),
        ("Name__123__123", 123, "Name__123__123"),
        ("Name__999", 123, "Name__999"),
        ("Name__internal_text", 123, "Name__internal_text"),
        ("Name\n__123", 123, "Name"),
        ("Name\t__123", 123, "Name"),
        ("Name\u202e__123", 123, "Name"),
        ("@Mod-ID - 123", 123, "@Mod-ID - 123"),
        ("Already Clean", 123, "Already Clean"),
    ],
)
def test_authoritative_suffix_cleaning_is_idempotent(raw, mod_id, expected):
    once = clean_display_mod_name(raw, mod_id)
    assert once == expected
    assert clean_display_mod_name(once, mod_id) == once


def test_cleaner_is_idempotent_for_problematic_and_legitimate_corpus():
    corpus = [
        "Server__123__456",
        "Controls\x00\n\t\x85Tail__456",
        "Bidi\u202e\u2066Tail\u2069__456",
        "Café 日本語 العربية فارسی\u200c क्\u200dष 👩\u200d💻__456",
        ("界👩\u200d💻" * 10000) + "__456",
        "Name__456__456",
        "\x00\u202e\u200b",
    ]
    for raw in corpus:
        once = clean_display_mod_name(raw, 456)
        assert clean_display_mod_name(once, 456) == once


@pytest.fixture
def isolated_metadata_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    metadata_path = cache_dir / "mod_metadata.json"
    monkeypatch.setattr(mod_metadata, "CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(mod_metadata, "MOD_METADATA_PATH", str(metadata_path))
    monkeypatch.setattr(mod_metadata, "_MOD_METADATA_LOCK", threading.RLock())
    return metadata_path


def test_cache_write_and_round_trip_normalize_and_bound_names(isolated_metadata_cache):
    raw = "Line\nNul\x00Bidi\u202eZero\u200b" + ("X" * 5000)
    expected = clean_display_mod_name(raw, 1001)

    mod_metadata.upsert_mod_metadata(1001, name=raw)

    persisted = json.loads(isolated_metadata_cache.read_text(encoding="utf-8"))
    assert persisted["mods"]["1001"]["name"] == expected
    assert len(expected) == MOD_DISPLAY_NAME_MAX_CHARS
    assert all(character not in expected for character in "\n\x00\u202e\u200b")
    assert mod_metadata.load_mod_metadata()["mods"]["1001"]["name"] == expected


@pytest.mark.parametrize("raw", ["", "\x00\u202e\u200b", "@Mod-ID - 1010", "1010", "@1010"])
def test_cache_update_does_not_promote_weak_display_names(
    isolated_metadata_cache, raw,
):
    mod_metadata.upsert_mod_metadata(1010, name=raw)

    persisted = json.loads(isolated_metadata_cache.read_text(encoding="utf-8"))
    assert "name" not in persisted["mods"]["1010"]
    assert "name" not in mod_metadata.load_mod_metadata()["mods"]["1010"]


def test_cache_update_persists_meaningful_normalized_name(isolated_metadata_cache):
    mod_metadata.upsert_mod_metadata(1011, name="Valid\nName\x00\u202e__1011")

    persisted = json.loads(isolated_metadata_cache.read_text(encoding="utf-8"))
    assert persisted["mods"]["1011"]["name"] == "Valid Name"
    assert mod_metadata.load_mod_metadata()["mods"]["1011"]["name"] == "Valid Name"


def test_historical_numeric_fallback_loads_but_remains_weak(isolated_metadata_cache):
    isolated_metadata_cache.parent.mkdir(parents=True)
    isolated_metadata_cache.write_text(json.dumps({
        "version": 1,
        "mods": {"1012": {"id": 1012, "name": "@Mod-ID - 1012"}},
    }), encoding="utf-8")

    loaded = mod_metadata.load_mod_metadata()
    assert loaded["mods"]["1012"]["name"] == "@Mod-ID - 1012"
    assert mod_name_resolver._is_weak_name(loaded["mods"]["1012"]["name"], 1012)


def test_cache_load_revalidates_historical_names_without_rewriting_file(
    isolated_metadata_cache,
):
    isolated_metadata_cache.parent.mkdir(parents=True)
    raw = "Old\nNul\x00Bidi\u202eZero\u200b" + ("Y" * 5000)
    original = json.dumps(
        {
            "version": 1,
            "mods": {
                "1002": {"id": 1002, "name": raw},
                "1003": {"id": 1003, "name": "\x00\u202e"},
            },
        }
    )
    isolated_metadata_cache.write_text(original, encoding="utf-8")

    loaded = mod_metadata.load_mod_metadata()

    assert loaded["mods"]["1002"]["name"] == clean_display_mod_name(raw)
    assert "name" not in loaded["mods"]["1003"]
    assert isolated_metadata_cache.read_text(encoding="utf-8") == original


def _server_json(mod_id: int, name: str) -> str:
    return json.dumps([{"steamWorkshopId": mod_id, "name": name}])


def test_server_name_extraction_boundaries_share_normalized_text():
    raw_name = "Remote\nName\x00\u202e\u200b" + ("R" * 5000)
    expected = clean_display_mod_name(raw_name)
    payload = _server_json(2001, raw_name)

    assert parse_mods_from_db(payload) == [(2001, expected)]
    assert _required_mod_names_from_json(payload) == [expected]
    assert parse_mods_preview(payload) == (1, expected)

    suggestion = build_mod_suggestion_index([payload])["mods"][0]
    assert suggestion["raw_names"] == (expected,)
    search_index = build_server_mod_index(payload)
    assert search_index["compact_names"] == frozenset(
        {"".join(character for character in expected.lower() if character.isascii() and character.isalnum())}
    )


def test_nested_suffix_converges_across_current_consumers(
    isolated_metadata_cache,
):
    mod_id = 456
    raw_name = "Server__123__456"
    expected = "Server__123"
    payload = _server_json(mod_id, raw_name)

    assert parse_mods_from_db(payload) == [(mod_id, expected)]
    assert _required_mod_names_from_json(payload) == [expected]
    assert parse_mods_preview(payload) == (1, expected)
    suggestion = build_mod_suggestion_index([payload])["mods"][0]
    assert suggestion["raw_names"] == (expected,)

    event = PreparationProgressEvent.from_authoritative_payload({
        "type": "item", "id": mod_id, "name": raw_name,
    })
    assert event.item_name == expected
    assert event.authoritative_payload()["name"] == expected
    outcome = PreparationOutcome(
        PreparationStatus.READY, verified_mods=((mod_id, raw_name),),
    )
    assert outcome.verified_mods == ((mod_id, expected),)

    mod_metadata.upsert_mod_metadata(mod_id, name=raw_name)
    assert mod_metadata.load_mod_metadata()["mods"][str(mod_id)]["name"] == expected


def test_actual_server_database_resolver_normalizes_before_caching(
    tmp_path, monkeypatch,
):
    database = tmp_path / "servers.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE servers (mods TEXT)")
    connection.execute(
        "INSERT INTO servers(mods) VALUES (?)",
        (_server_json(2002, "Server\nName\u202e"),),
    )
    connection.commit()
    connection.close()
    cached = []
    monkeypatch.setattr(mod_name_resolver, "DB_LOCAL_PATH", str(database))
    monkeypatch.setattr(
        mod_name_resolver,
        "upsert_mod_metadata",
        lambda mod_id, *, name: cached.append((mod_id, name)),
    )

    resolved = mod_name_resolver.resolve_best_mod_names(
        [2002], metadata={}, workshop_roots=[], symlink_names={},
    )

    assert resolved == {2002: "Server Name"}
    assert cached == [(2002, "Server Name")]


def test_resolver_falls_through_when_normalized_source_is_empty(monkeypatch, tmp_path):
    root = tmp_path / "workshop"
    item = root / "content/221100/2003"
    item.mkdir(parents=True)
    (item / "meta.cpp").write_text('name = "Local Valid";\n', encoding="utf-8")
    monkeypatch.setattr(mod_name_resolver, "names_from_server_db", lambda _ids: {})
    monkeypatch.setattr(mod_name_resolver, "upsert_mod_metadata", lambda *_a, **_k: None)

    assert mod_name_resolver.resolve_best_mod_names(
        [2003],
        metadata={"2003": {"name": "\x00\u202e\u200b"}},
        workshop_roots=[root],
        symlink_names={},
    ) == {2003: "Local Valid"}


def test_long_local_metadata_is_bounded_by_actual_parser_and_resolver(
    monkeypatch, tmp_path,
):
    root = tmp_path / "workshop"
    item = root / "content/221100/2004"
    item.mkdir(parents=True)
    (item / "meta.cpp").write_text(
        f'name = "{"L" * 30000}";\n', encoding="utf-8",
    )
    monkeypatch.setattr(mod_name_resolver, "names_from_server_db", lambda _ids: {})
    monkeypatch.setattr(mod_name_resolver, "upsert_mod_metadata", lambda *_a, **_k: None)

    assert mod_name_resolver.name_from_local_metadata(
        2004, workshop_roots=[root],
    ) == "L" * MOD_DISPLAY_NAME_MAX_CHARS


def test_symlink_display_fallback_is_normalized_without_renaming_source(
    monkeypatch,
):
    raw_name = "@Local\nBidi\u202eZero\u200b__2005"
    monkeypatch.setattr(mod_name_resolver, "names_from_server_db", lambda _ids: {})
    monkeypatch.setattr(mod_name_resolver, "upsert_mod_metadata", lambda *_a, **_k: None)

    resolved = mod_name_resolver.resolve_best_mod_names(
        [2005], metadata={}, workshop_roots=[], symlink_names={2005: raw_name},
    )

    assert resolved == {2005: "@Local BidiZero"}
    assert raw_name == "@Local\nBidi\u202eZero\u200b__2005"


def _installed_workshop_fixture(tmp_path: Path, mod_id: int) -> Path:
    workshop = tmp_path / "workshop"
    addons = workshop / "content" / "221100" / str(mod_id) / "addons"
    addons.mkdir(parents=True)
    (addons / "content.pbo").write_bytes(b"content")
    return workshop


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("ordinary", "Ordinary Mod"),
        ("long-ascii", "A" * 5000),
        ("long-multibyte", "界👩\u200d💻" * 2000),
        ("empty-after-normalization", "\x00\u202e\u200b"),
    ],
)
def test_real_watch_symlink_creation_uses_byte_safe_deterministic_name(
    tmp_path, kind, name,
):
    mod_id = 3001
    workshop = _installed_workshop_fixture(tmp_path / kind, mod_id)
    watch = tmp_path / kind / "watch"

    result = ensure_watch_symlinks(
        workshop_dir=str(workshop),
        mods=[(mod_id, name)],
        watch_folder=str(watch),
        cleanup_stale=False,
    )

    assert result["errors"] == []
    assert len(result["created"]) == len(result["selected_paths"]) == 1
    component = Path(result["selected_paths"][0]).name
    assert component.endswith(f"__{mod_id}")
    assert len(component.encode("utf-8")) <= os.pathconf(watch, "PC_NAME_MAX")
    assert Path(result["selected_paths"][0]).is_symlink()
    assert component == symlink_name_for_mod(
        name, mod_id, name_max=os.pathconf(watch, "PC_NAME_MAX"),
    )
    assert all(character not in component for character in "\n\r\t\x00\u202e\u200b\u2060\ufeff")


@pytest.mark.parametrize("mod_id", [3002, (2**64) - 1])
def test_watch_component_preserves_suffix_at_normal_name_max(mod_id):
    component = symlink_name_for_mod("界" * 5000, mod_id, name_max=255)
    assert component.endswith(f"__{mod_id}")
    assert len(component.encode("utf-8")) <= 255


def test_watch_component_exact_and_impossible_name_max_boundaries():
    mod_id = (2**64) - 1
    minimum = f"@Mod__{mod_id}"
    minimum_bytes = len(minimum.encode("utf-8"))

    exact = symlink_name_for_mod("\x00\u202e\u200b", mod_id, name_max=minimum_bytes)
    assert exact == minimum
    sufficient = symlink_name_for_mod("Long Name", mod_id, name_max=minimum_bytes + 1)
    assert sufficient.endswith(f"__{mod_id}")
    assert len(sufficient.encode("utf-8")) <= minimum_bytes + 1
    with pytest.raises(ValueError, match="cannot fit required watch-link name"):
        symlink_name_for_mod("Name", mod_id, name_max=minimum_bytes - 1)
    with pytest.raises(ValueError, match="cannot fit required watch-link name"):
        symlink_name_for_mod("Name", mod_id, name_max=1)


def test_watch_name_max_pathconf_failure_uses_255_fallback(tmp_path, monkeypatch):
    mod_id = 3003
    workshop = _installed_workshop_fixture(tmp_path, mod_id)
    watch = tmp_path / "watch"
    monkeypatch.setattr(
        steamcmd_mods.os, "pathconf",
        lambda *_args: (_ for _ in ()).throw(OSError("unsupported")),
    )

    result = ensure_watch_symlinks(
        workshop_dir=str(workshop), mods=[(mod_id, "界" * 5000)],
        watch_folder=str(watch), cleanup_stale=False,
    )

    assert result["errors"] == []
    component = Path(result["created"][0]).name
    assert component.endswith(f"__{mod_id}")
    assert len(component.encode("utf-8")) <= 255


def test_impossible_name_max_creates_no_link_and_touches_no_existing_link(
    tmp_path, monkeypatch,
):
    mod_id = 3004
    workshop = _installed_workshop_fixture(tmp_path, mod_id)
    watch = tmp_path / "watch"
    watch.mkdir()
    unrelated_target = tmp_path / "unrelated"
    unrelated_target.mkdir()
    unrelated = watch / "@Unrelated__9999"
    unrelated.symlink_to(unrelated_target, target_is_directory=True)
    monkeypatch.setattr(steamcmd_mods, "_watch_name_max", lambda _path: 1)

    result = ensure_watch_symlinks(
        workshop_dir=str(workshop), mods=[(mod_id, "Name")],
        watch_folder=str(watch), cleanup_stale=True,
    )

    assert len(result["errors"]) == 1
    assert "NAME_MAX 1" in result["errors"][0]
    assert result["created"] == result["updated"] == result["removed"] == []
    assert result["selected_paths"] == []
    assert unrelated.is_symlink()


def test_join_contract_and_status_reducer_use_normalized_name():
    raw_name = "Join\nName\x00\u202e\u200b" + ("J" * 5000)
    expected = clean_display_mod_name(raw_name)
    outcome = PreparationOutcome(
        PreparationStatus.READY, verified_mods=((4001, raw_name),),
    )
    assert outcome.verified_mods == ((4001, expected),)

    reducer = PreparationPresentationReducer(
        operation_id=41, is_current=lambda operation_id: operation_id == 41,
    )

    def apply(payload):
        return reducer.apply(PreparationProgressEvent.from_authoritative_payload(payload))

    apply({"type": "presentation_work_set", "join_attempt_id": 41, "work_ids": [4001]})
    base = {
        "type": "item",
        "join_attempt_id": 41,
        "backend": "steam_ugc",
        "backend_owner": "steam_client",
        "id": 4001,
        "name": raw_name,
        "total_bytes": 100,
    }
    apply(base | {"event_source": "initial", "installed": False, "download_bytes": 0})
    apply(base | {
        "event_source": "request",
        "was_installed_before": False,
        "request_attempted": True,
        "request_accepted": True,
        "download_bytes": 0,
    })
    reduction = apply(base | {
        "event_source": "callback",
        "was_installed_before": False,
        "download_bytes": 10,
    })

    assert reduction.snapshot is not None
    assert reduction.snapshot.current_mod_name == expected
    assert expected in reduction.snapshot.stage_text


def test_preparation_event_payload_and_compatibility_presenter_share_name():
    raw_name = "Event\nName\u202e__4002"
    expected = "Event Name"
    event = PreparationProgressEvent.from_authoritative_payload({
        "type": "item",
        "join_attempt_id": 42,
        "backend": "steam_ugc",
        "backend_owner": "steam_client",
        "id": 4002,
        "name": raw_name,
        "download_bytes": 10,
        "total_bytes": 100,
        "reason": "unchanged",
    })
    consumed = []
    JoinPopupPreparationPresenter(consume_event=consumed.append).on_event(event)

    assert event.item_name == expected
    assert event.authoritative_payload()["name"] == expected
    assert consumed[0]["name"] == expected
    assert consumed[0]["id"] == 4002
    assert consumed[0]["download_bytes"] == 10
    assert consumed[0]["total_bytes"] == 100
    assert consumed[0]["reason"] == "unchanged"


def test_mod_manager_filter_and_sort_receive_normalized_names():
    normalized = clean_display_mod_name("LineA\nLineB\u202e\u200b")
    row = object()
    manager = ModsManagerOverlay.__new__(ModsManagerOverlay)
    manager._rows_cache = [(row, normalized.lower(), "5001")]
    manager.search = type("Search", (), {"get_text": lambda self: "linea lineb"})()
    assert manager._filter_row(row) is True

    manager.sort_key = "name"
    manager.sort_ascending = True
    sorted_items = manager._sorted_items(
        [(clean_display_mod_name("\u202eBeta"), 5002), ("Alpha", 5001)],
    )
    assert sorted_items == [("Alpha", 5001), ("Beta", 5002)]


def test_headless_pango_layout_receives_single_line_bounded_text():
    cairo = pytest.importorskip("cairo")
    gi = pytest.importorskip("gi")
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo

    normalized = clean_display_mod_name(
        "LineA\nLineB\x00\u202e\u2066\u2069\u200b" + ("P" * 5000),
    )
    context = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 600, 100))
    layout = PangoCairo.create_layout(context)
    layout.set_text(normalized, -1)

    assert layout.get_line_count() == 1
    assert layout.get_text() == normalized
    assert len(normalized) <= MOD_DISPLAY_NAME_MAX_CHARS
    assert all(character not in normalized for character in "\n\x00\u202e\u2066\u2069\u200b")
