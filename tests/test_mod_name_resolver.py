from pathlib import Path

import pytest

from dzll_launcher import mod_name_resolver, mods_ui, steam_native
from dzll_launcher.mods_ui import ModsManagerOverlay
from dzll_launcher.steam_native import SteamClientState


def _item_dir(root: Path, mod_id: int) -> Path:
    path = root / "content" / "221100" / str(mod_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_metadata(
    root: Path,
    mod_id: int,
    name: str,
    *,
    filename: str = "meta.cpp",
) -> Path:
    path = _item_dir(root, mod_id) / filename
    path.write_text(f'name = "{name}";\n', encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate_non_local_name_sources(monkeypatch):
    monkeypatch.setattr(mod_name_resolver, "names_from_server_db", lambda _ids: {})
    monkeypatch.setattr(mod_name_resolver, "upsert_many_names", lambda *_args, **_kwargs: None)


def test_single_workshop_dir_compatibility_resolves_meta_cpp(tmp_path):
    root = tmp_path / "workshop"
    _write_metadata(root, 1001, "Baseline Name")

    assert mod_name_resolver.resolve_best_mod_names(
        [1001], metadata={}, workshop_dir=str(root),
    ) == {1001: "Baseline Name"}


def test_multiple_roots_resolve_item_present_only_in_primary(tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    _write_metadata(primary, 1002, "Primary Name")

    assert mod_name_resolver.resolve_best_mod_names(
        [1002], metadata={}, workshop_roots=[primary, secondary],
    ) == {1002: "Primary Name"}


def test_multiple_roots_resolve_item_present_only_in_secondary(tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    _write_metadata(secondary, 1003, "Secondary Name")

    assert mod_name_resolver.resolve_best_mod_names(
        [1003], metadata={}, workshop_roots=[primary, secondary],
    ) == {1003: "Secondary Name"}


def test_multiple_roots_resolve_items_split_between_roots(tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    _write_metadata(primary, 1004, "Split Primary")
    _write_metadata(secondary, 1005, "Split Secondary")

    assert mod_name_resolver.resolve_best_mod_names(
        [1004, 1005], metadata={}, workshop_roots=[primary, secondary],
    ) == {
        1004: "Split Primary",
        1005: "Split Secondary",
    }


def test_missing_primary_metadata_continues_to_secondary(tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    _item_dir(primary, 1006)
    _write_metadata(secondary, 1006, "Secondary After Missing")

    assert mod_name_resolver.name_from_local_metadata(
        1006, workshop_roots=[primary, secondary],
    ) == "Secondary After Missing"


@pytest.mark.parametrize(
    "primary_text",
    [
        'name = "unterminated"\n',
        'name = "@Mod-ID - 1007";\n',
        'title = "1007";\n',
    ],
    ids=["malformed", "weak-fallback", "weak-numeric"],
)
def test_unusable_primary_metadata_continues_to_secondary(
    tmp_path, primary_text,
):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    (_item_dir(primary, 1007) / "meta.cpp").write_text(
        primary_text, encoding="utf-8",
    )
    _write_metadata(secondary, 1007, "Secondary Valid")

    assert mod_name_resolver.name_from_local_metadata(
        1007, workshop_roots=[primary, secondary],
    ) == "Secondary Valid"


def test_unreadable_primary_metadata_continues_to_secondary(monkeypatch, tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    unreadable = _write_metadata(primary, 1008, "Unreadable Primary")
    _write_metadata(secondary, 1008, "Secondary After Read Error")
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("isolated unreadable fixture")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert mod_name_resolver.name_from_local_metadata(
        1008, workshop_roots=[primary, secondary],
    ) == "Secondary After Read Error"


def test_duplicate_item_uses_first_ordered_root_and_reverses_deterministically(tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    _write_metadata(primary, 1009, "Primary Duplicate")
    _write_metadata(secondary, 1009, "Secondary Duplicate")

    assert mod_name_resolver.name_from_local_metadata(
        1009, workshop_roots=[primary, secondary],
    ) == "Primary Duplicate"
    assert mod_name_resolver.name_from_local_metadata(
        1009, workshop_roots=[secondary, primary],
    ) == "Secondary Duplicate"


def test_mod_cpp_is_used_when_meta_cpp_is_absent(tmp_path):
    root = tmp_path / "workshop"
    _write_metadata(root, 1010, "Mod Cpp Name", filename="mod.cpp")

    assert mod_name_resolver.name_from_local_metadata(
        1010, workshop_roots=[root],
    ) == "Mod Cpp Name"


def test_meta_cpp_precedes_mod_cpp_within_root(tmp_path):
    root = tmp_path / "workshop"
    _write_metadata(root, 1011, "Meta Cpp Name")
    _write_metadata(root, 1011, "Mod Cpp Name", filename="mod.cpp")

    assert mod_name_resolver.name_from_local_metadata(
        1011, workshop_roots=[root],
    ) == "Meta Cpp Name"


def test_symlinked_metadata_file_is_skipped_for_mod_cpp(tmp_path):
    root = tmp_path / "workshop"
    item = _item_dir(root, 1012)
    external = tmp_path / "external-meta.cpp"
    external.write_text('name = "Linked Meta";\n', encoding="utf-8")
    (item / "meta.cpp").symlink_to(external)
    _write_metadata(root, 1012, "Real Mod Cpp", filename="mod.cpp")

    assert mod_name_resolver.name_from_local_metadata(
        1012, workshop_roots=[root],
    ) == "Real Mod Cpp"


def test_symlinked_item_directory_is_skipped_for_later_root(tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    primary_parent = primary / "content/221100"
    primary_parent.mkdir(parents=True)
    external_item = tmp_path / "external-item"
    external_item.mkdir()
    (external_item / "meta.cpp").write_text(
        'name = "Linked Item";\n', encoding="utf-8",
    )
    (primary_parent / "1013").symlink_to(external_item, target_is_directory=True)
    _write_metadata(secondary, 1013, "Real Secondary Item")

    assert mod_name_resolver.name_from_local_metadata(
        1013, workshop_roots=[primary, secondary],
    ) == "Real Secondary Item"


def test_no_usable_local_metadata_preserves_symlink_then_numeric_fallback(tmp_path):
    root = tmp_path / "workshop"

    assert mod_name_resolver.resolve_best_mod_names(
        [1014, 1015],
        metadata={},
        workshop_roots=[root],
        symlink_names={1014: "@Watch Name__1014"},
    ) == {
        1014: "@Watch Name",
        1015: "@Mod-ID - 1015",
    }


def test_strong_cache_wins_over_all_local_roots(tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    _write_metadata(primary, 1016, "Primary Local")
    _write_metadata(secondary, 1016, "Secondary Local")

    assert mod_name_resolver.resolve_best_mod_names(
        [1016],
        metadata={"1016": {"name": "Strong Cached Name"}},
        workshop_roots=[primary, secondary],
    ) == {1016: "Strong Cached Name"}


def test_weak_cached_fallback_does_not_block_secondary_local_name(tmp_path):
    primary = tmp_path / "primary/workshop"
    secondary = tmp_path / "secondary/workshop"
    _write_metadata(secondary, 1017, "Secondary Beats Weak Cache")

    assert mod_name_resolver.resolve_best_mod_names(
        [1017],
        metadata={"1017": {"name": "@Mod-ID - 1017"}},
        workshop_roots=[primary, secondary],
    ) == {1017: "Secondary Beats Weak Cache"}


def _libraryfolders_text(libraries) -> str:
    entries = []
    for index, library in enumerate(libraries):
        entries.append(
            f'\t"{index}"\n'
            "\t{\n"
            f'\t\t"path"\t\t"{library}"\n'
            "\t}"
        )
    return '"libraryfolders"\n{\n' + "\n".join(entries) + "\n}\n"


def _configure_real_mod_manager_libraries(monkeypatch, tmp_path):
    primary = tmp_path / "PrimarySteam"
    dayz_library = tmp_path / "DayZLibrary"
    for library in (primary, dayz_library):
        (library / "steamapps").mkdir(parents=True)
    (primary / "steamapps/libraryfolders.vdf").write_text(
        _libraryfolders_text([primary, dayz_library]), encoding="utf-8",
    )
    (dayz_library / "steamapps/appmanifest_221100.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"221100"\n}\n', encoding="utf-8",
    )

    monkeypatch.setattr(steam_native, "resolve_native_steam_root", lambda: primary)
    monkeypatch.setattr(mods_ui, "dayz_steam_library", steam_native.dayz_steam_library)
    monkeypatch.setattr(
        mods_ui, "dayz_workshop_content_dir", steam_native.dayz_workshop_content_dir,
    )
    monkeypatch.setattr(mods_ui, "native_steam_libraries", steam_native.native_steam_libraries)
    monkeypatch.setattr(
        mods_ui,
        "autodetect_workshop_dir",
        lambda: str(primary / "steamapps/workshop"),
    )
    monkeypatch.setattr(mods_ui, "_name_map_from_symlinks", lambda **_kwargs: {})
    monkeypatch.setattr(mods_ui, "load_mod_metadata", lambda: {"mods": {}})
    return primary, dayz_library


def test_mod_manager_inventory_resolves_secondary_library_local_name(
    monkeypatch, tmp_path,
):
    primary, dayz_library = _configure_real_mod_manager_libraries(
        monkeypatch, tmp_path,
    )
    dayz_root = dayz_library / "steamapps/workshop"
    _write_metadata(dayz_root, 2001, "Secondary Real Name")

    configured_root = primary / "steamapps/workshop"
    roots = mods_ui._candidate_workshop_roots(workshop_dir=str(configured_root))
    items = ModsManagerOverlay.__new__(ModsManagerOverlay)._load_installed_items(
        str(configured_root), "", steam_state=SteamClientState.OFFLINE,
    )

    assert roots == [dayz_root.resolve(), configured_root.resolve()]
    assert [(item[1], item[0], item[3]) for item in items] == [
        (2001, "Secondary Real Name", True),
    ]


def test_mod_manager_duplicate_name_uses_dayz_owning_library_first(
    monkeypatch, tmp_path,
):
    primary, dayz_library = _configure_real_mod_manager_libraries(
        monkeypatch, tmp_path,
    )
    primary_root = primary / "steamapps/workshop"
    dayz_root = dayz_library / "steamapps/workshop"
    _write_metadata(primary_root, 2002, "Primary Steam Root Name")
    _write_metadata(dayz_root, 2002, "DayZ Library Name")

    roots = mods_ui._candidate_workshop_roots(workshop_dir=str(primary_root))
    items = ModsManagerOverlay.__new__(ModsManagerOverlay)._load_installed_items(
        str(primary_root), "", steam_state=SteamClientState.OFFLINE,
    )

    assert roots == [dayz_root.resolve(), primary_root.resolve()]
    assert mods_ui._content_folder_for_mod(roots, 2002) == (
        dayz_root / "content/221100/2002"
    )
    assert [(item[1], item[0], item[3]) for item in items] == [
        (2002, "DayZ Library Name", True),
    ]


def test_mod_manager_inventory_normalizes_local_name_before_search_and_sort(
    monkeypatch, tmp_path,
):
    primary, dayz_library = _configure_real_mod_manager_libraries(
        monkeypatch, tmp_path,
    )
    dayz_root = dayz_library / "steamapps/workshop"
    _write_metadata(dayz_root, 2003, "LineA\nLineB\u202e\u200b")

    items = ModsManagerOverlay.__new__(ModsManagerOverlay)._load_installed_items(
        str(primary / "steamapps/workshop"),
        "",
        steam_state=SteamClientState.OFFLINE,
    )

    assert [(item[1], item[0], item[3]) for item in items] == [
        (2003, "LineA LineB", True),
    ]
