from pathlib import Path

import pytest

from dzll_launcher import steam_native, steam_ugc_backend


def _vdf_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _libraryfolders_text(values: list[str]) -> str:
    entries = []
    for index, value in enumerate(values):
        entries.append(
            f'\t"{index}"\n'
            "\t{\n"
            f'\t\t"path"\t\t"{value}"\n'
            "\t}"
        )
    return '"libraryfolders"\n{\n' + "\n".join(entries) + "\n}\n"


@pytest.mark.parametrize(
    ("suffix", "vdf_encoding"),
    [
        ("SteamLibrary", "escaped"),
        ("My Steam Library", "escaped"),
        ("Spiele-äöü", "escaped"),
        ("ゲーム", "escaped"),
        ("Spiele-äöü-ゲーム", "escaped"),
        (r"escaped\backslash", "escaped"),
        ('escaped"quote', "escaped"),
        (r"literal\name", "literal"),
        (r"literal\table", "literal"),
        (r"literal\root", "literal"),
        (r"literal\x41tail", "literal"),
        (r"literal\u0041tail", "literal"),
        (r"literal\qtail", "literal"),
        (r"multiple\\backslashes\\\tail", "escaped"),
    ],
    ids=[
        "normal",
        "spaces",
        "unicode-latin",
        "unicode-japanese",
        "unicode-mixed",
        "vdf-escaped-backslash",
        "vdf-escaped-quote",
        "literal-backslash-n",
        "literal-backslash-t",
        "literal-backslash-r",
        "literal-backslash-x41",
        "literal-backslash-u0041",
        "unknown-escape",
        "multiple-backslashes",
    ],
)
def test_libraryfolders_preserves_exact_filesystem_path(
        tmp_path, suffix, vdf_encoding):
    intended = tmp_path / suffix
    raw_value = str(intended)
    encoded_value = (
        _vdf_escape(raw_value) if vdf_encoding == "escaped" else raw_value
    )
    fixture = tmp_path / "libraryfolders.vdf"
    fixture.write_text(
        _libraryfolders_text([encoded_value]), encoding="utf-8",
    )

    parsed = steam_native._parse_vdf(fixture.read_text(encoding="utf-8"))
    parsed_value = parsed["libraryfolders"]["0"]["path"]
    discovered = steam_native.parse_steam_libraryfolders_vdf(fixture)

    assert parsed_value == raw_value
    assert discovered == [intended.resolve()]
    assert str(discovered[0]) == str(intended.resolve())


def test_libraryfolders_preserves_multiple_libraries(tmp_path):
    libraries = [
        tmp_path / "Primary Steam Library",
        tmp_path / "Spiele-äöü",
        tmp_path / "ゲーム",
    ]
    fixture = tmp_path / "libraryfolders.vdf"
    fixture.write_text(
        _libraryfolders_text([_vdf_escape(str(path)) for path in libraries]),
        encoding="utf-8",
    )

    assert steam_native.parse_steam_libraryfolders_vdf(fixture) == [
        path.resolve() for path in libraries
    ]


def test_unicode_library_and_installdir_resolve_end_to_end(
        monkeypatch, tmp_path):
    steam_root = tmp_path / "Native Steam"
    library = tmp_path / "Spiele-äöü-ゲーム"
    installdir = "DayZ-Überleben-ゲーム"
    (steam_root / "steamapps").mkdir(parents=True)
    (library / "steamapps/common" / installdir).mkdir(parents=True)
    (library / "steamapps/workshop/content/221100").mkdir(parents=True)
    (library / "steamapps/workshop/downloads/221100").mkdir(parents=True)
    (library / "steamapps/compatdata/221100").mkdir(parents=True)
    (steam_root / "steamapps/libraryfolders.vdf").write_text(
        _libraryfolders_text([_vdf_escape(str(library))]),
        encoding="utf-8",
    )
    (library / "steamapps/appmanifest_221100.acf").write_text(
        '"AppState"\n{\n'
        '\t"appid"\t\t"221100"\n'
        f'\t"installdir"\t\t"{_vdf_escape(installdir)}"\n'
        '}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        steam_native, "resolve_native_steam_root", lambda: steam_root,
    )

    assert steam_native.native_steam_libraries() == [
        steam_root.resolve(), library.resolve(),
    ]
    assert steam_native.dayz_steam_library() == library.resolve()
    assert steam_native.dayz_install_dir() == (
        library.resolve() / "steamapps/common" / installdir
    )
    assert steam_native.dayz_workshop_content_dir() == (
        library.resolve() / "steamapps/workshop/content/221100"
    )
    assert steam_native.dayz_workshop_downloads_dir() == (
        library.resolve() / "steamapps/workshop/downloads/221100"
    )
    assert steam_native.dayz_compatdata_dir() == (
        library.resolve() / "steamapps/compatdata/221100"
    )


@pytest.mark.parametrize(
    "installdir",
    [
        "DayZ",
        "DayZ-ゲーム",
        'DayZ-"quoted"',
        'DayZ-äöü-"quoted"',
        r"DayZ\backslash",
        r"DayZ\\many\\\backslashes",
        r"DayZ\name",
        r"DayZ\table",
        r"DayZ\x41",
        r"DayZ\u0041",
        r"DayZ\q",
        "DayZ-trailing\\",
    ],
    ids=[
        "plain",
        "unicode",
        "escaped-quote",
        "unicode-escaped-quote",
        "escaped-backslash",
        "multiple-backslashes",
        "literal-backslash-n",
        "literal-backslash-t",
        "literal-backslash-x41",
        "literal-backslash-u0041",
        "unknown-escape",
        "trailing-backslash",
    ],
)
def test_appmanifest_regex_fallback_preserves_exact_installdir(
        tmp_path, installdir):
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "appmanifest_221100.acf").write_text(
        f'"installdir"\t\t"{_vdf_escape(installdir)}"\n',
        encoding="utf-8",
    )

    assert steam_native._steam_app_installdir(tmp_path, 221100) == installdir


@pytest.mark.parametrize(
    "installdir",
    [
        "DayZ",
        "DayZ-ゲーム",
        'DayZ-"quoted"',
        'DayZ-äöü-"quoted"',
        r"DayZ\backslash",
        r"DayZ\\many\\\backslashes",
        r"DayZ\name",
        r"DayZ\table",
        r"DayZ\x41",
        r"DayZ\u0041",
        r"DayZ\q",
        "DayZ-trailing\\",
    ],
)
def test_appmanifest_parser_and_regex_fallback_have_identical_semantics(
        tmp_path, installdir):
    parsed_library = tmp_path / "parsed"
    fallback_library = tmp_path / "fallback"
    for library in (parsed_library, fallback_library):
        (library / "steamapps").mkdir(parents=True)
    encoded = _vdf_escape(installdir)
    (parsed_library / "steamapps/appmanifest_221100.acf").write_text(
        '"AppState"\n{\n'
        f'\t"installdir"\t\t"{encoded}"\n'
        '}\n',
        encoding="utf-8",
    )
    (fallback_library / "steamapps/appmanifest_221100.acf").write_text(
        f'"installdir"\t\t"{encoded}"\n', encoding="utf-8",
    )

    parsed_result = steam_native._steam_app_installdir(
        parsed_library, 221100,
    )
    fallback_result = steam_native._steam_app_installdir(
        fallback_library, 221100,
    )

    assert parsed_result == installdir
    assert fallback_result == installdir
    assert parsed_result == fallback_result


def test_unicode_library_does_not_trust_old_mojibake_sibling(
        monkeypatch, tmp_path):
    steam_root = tmp_path / "Native Steam"
    intended = tmp_path / "Spiele-äöü"
    old_mojibake = tmp_path / "Spiele-Ã¤Ã¶Ã¼"
    for root in (steam_root, intended, old_mojibake):
        (root / "steamapps").mkdir(parents=True)
    (steam_root / "steamapps/libraryfolders.vdf").write_text(
        _libraryfolders_text([_vdf_escape(str(intended))]),
        encoding="utf-8",
    )
    intended_target = intended / "steamapps/workshop/content/221100/424242"
    wrong_target = old_mojibake / "steamapps/workshop/content/221100/424242"
    intended_target.mkdir(parents=True)
    wrong_target.mkdir(parents=True)
    monkeypatch.setattr(
        steam_native, "resolve_native_steam_root", lambda: steam_root,
    )

    libraries = steam_native.native_steam_libraries()
    assert intended.resolve() in libraries
    assert old_mojibake.resolve() not in libraries
    assert intended.resolve() in steam_ugc_backend._native_steam_roots()
    assert old_mojibake.resolve() not in steam_ugc_backend._native_steam_roots()
    assert steam_ugc_backend._safe_workshop_content_path(
        str(intended_target), mod_id=424242, appid=221100,
    ) == intended_target
    assert steam_ugc_backend._safe_workshop_content_path(
        str(wrong_target), mod_id=424242, appid=221100,
    ) is None
