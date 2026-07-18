from dzll_launcher import column_view


def test_lowercase_names_are_capitalised_and_sorted() -> None:
    names = ["namalsk survival", "community framework", "advanced groups"]

    assert column_view._prepare_required_mod_display_names(names) == [
        "Advanced Groups",
        "Community Framework",
        "Namalsk Survival",
    ]


def test_sorting_is_case_insensitive_with_deterministic_ties() -> None:
    names = ["zulu", "beta", "Alpha", "alpha", "Beta"]

    assert column_view._prepare_required_mod_display_names(names) == [
        "Alpha",
        "Alpha",
        "Beta",
        "Beta",
        "Zulu",
    ]


def test_internal_capitals_and_acronyms_are_preserved() -> None:
    names = ["PvE AI patrols", "DayZ VPP tools"]

    assert column_view._prepare_required_mod_display_names(names) == [
        "DayZ VPP Tools",
        "PvE AI Patrols",
    ]


def test_hyphenated_and_punctuated_names_capitalise_word_starts() -> None:
    names = ["@community_framework", "dayz-expansion-core", "[test] d'artagnan"]

    assert column_view._prepare_required_mod_display_names(names) == [
        "@Community_Framework",
        "[Test] D'Artagnan",
        "Dayz-Expansion-Core",
    ]


def test_one_column_list_is_sorted() -> None:
    columns = column_view._split_required_mod_display_names(["charlie", "alpha", "bravo"])

    assert columns == [["Alpha", "Bravo", "Charlie"]]


def test_two_column_list_is_globally_sorted_before_split() -> None:
    names = ["lima", "kilo", "juliet", "india", "hotel", "golf", "foxtrot",
             "echo", "delta", "charlie", "bravo", "alpha"]

    assert column_view._split_required_mod_display_names(names) == [
        ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"],
        ["Golf", "Hotel", "India", "Juliet", "Kilo", "Lima"],
    ]


def test_duplicate_names_have_deterministic_stable_output() -> None:
    names = ["same", "Same", "SAME"]

    first = column_view._prepare_required_mod_display_names(names)
    second = column_view._prepare_required_mod_display_names(names)
    assert first == second == ["SAME", "Same", "Same"]


def test_empty_mod_list_does_not_show_popup(monkeypatch) -> None:
    class EmptyTarget:
        _dzll_required_mod_names: list[str] = []

    closed = []
    monkeypatch.setattr(column_view, "_popdown_required_mods_popover", closed.append)
    target = EmptyTarget()

    column_view._show_required_mods_popover(target)

    assert closed == [target]


def test_original_mod_names_are_not_mutated() -> None:
    names = ["zulu mod", "PvE tools", "alpha-mod"]
    original = list(names)

    column_view._split_required_mod_display_names(names)

    assert names == original
