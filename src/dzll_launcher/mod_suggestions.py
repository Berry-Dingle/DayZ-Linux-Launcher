#!/usr/bin/env python3
import json
import re
from typing import Any, Iterable

from .mod_alias import alias_values_for, is_exact_alias
from .mod_search import compact_mod_text, normalize_mod_text


SUGGESTION_HINTS = {
    "ai": ("DayZ-Expansion-AI", "InediaInfectedAI", "Airborne AI", "AI Bandits", "AI War Zones", "AI ABDUCTORS"),
    "bbp": ("BaseBuildingPlus", "BBPItemPack"),
    "bf": ("Building Fortifications",),
    "cl": ("CodeLock", "Code Lock"),
}
SUGGESTION_SHORTHANDS = frozenset(("bbp", "bf", "cf", "cl", "cot", "df", "fog", "kgb", "mmg", "rag", "rf", "sip", "snafu", "vpp", "vppm", "vsm"))
CANONICAL_MOD_DISPLAY_NAMES = {
    "1559212036": "Community Framework",
    "1646187754": "Code Lock",
    "1828439124": "VPPAdminTools",
    "2545327648": "Dabs Framework",
    "1710977250": "BaseBuildingPlus",
    "2670506982": "Building Fortifications",
    "1564026768": "Community Online Tools",
    "1623711988": "VanillaPlusPlusMap",
    "2443122116": "SNAFU Weapons",
    "2931560672": "Forward Operator Gear",
}
CANONICAL_MOD_DISPLAY_NAMES_BY_COMPACT = {
    "cf": "Community Framework",
    "communityframework": "Community Framework",
    "codelock": "Code Lock",
    "vppadmintools": "VPPAdminTools",
    "dabsframework": "Dabs Framework",
    "basebuildingplus": "BaseBuildingPlus",
    "buildingfortifications": "Building Fortifications",
    "communityonlinetools": "Community Online Tools",
    "vanillaplusplusmap": "VanillaPlusPlusMap",
    "snafu": "SNAFU Weapons",
    "snafuweapons": "SNAFU Weapons",
    "forwardoperatorgear": "Forward Operator Gear",
}
_MOD_OPERATOR_RE = re.compile(r"(^|\s)(mods:|-mods=)", re.IGNORECASE)


class _ModSuggestionIndex(dict):
    def __len__(self) -> int:
        return len(self.get("mods", ()))


def _clean_workshop_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def _mod_identity(name: str, workshop_id: str) -> str:
    if workshop_id:
        return f"id:{workshop_id}"
    compact_name = compact_mod_text(name)
    if compact_name:
        return f"name:{compact_name}"
    return f"name:{normalize_mod_text(name)}"


def _empty_index() -> dict[str, Any]:
    return _ModSuggestionIndex({"mods": (), "count": 0, "cache": {}, "prefix_map": {}, "exact_compact_map": {}})


def _dedupe(values: list[str]) -> tuple[str, ...]:
    out = []
    seen = set()
    for value in values:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return tuple(out)


def _parse_mods_json(mods_json: Any) -> list[dict[str, Any]]:
    if not mods_json:
        return []

    try:
        parsed = json.loads(str(mods_json))
    except Exception:
        return []

    if not isinstance(parsed, list):
        return []

    return [item for item in parsed if isinstance(item, dict)]


def display_mod_name(name: Any) -> str:
    raw = str(name or "").strip()
    text = re.sub(r"[_\-\s]+", " ", raw).strip()
    if not text:
        return raw

    words = []
    for word in text.split():
        if any(ch.isupper() for ch in word) or any(ch.isdigit() for ch in word):
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _choose_display_name(name_counts: dict[str, int]) -> str:
    if not name_counts:
        return ""
    name = sorted(name_counts, key=lambda value: (-name_counts[value], len(value), value.casefold()))[0]
    return display_mod_name(name)


def _canonical_display_name(workshop_id: str, names: tuple[str, ...]) -> str:
    canonical_name = CANONICAL_MOD_DISPLAY_NAMES.get(workshop_id, "")
    if canonical_name:
        return canonical_name
    for name in names:
        canonical_name = CANONICAL_MOD_DISPLAY_NAMES_BY_COMPACT.get(compact_mod_text(name), "")
        if canonical_name:
            return canonical_name
    return ""


def _build_entry(names: tuple[str, ...], name_counts: dict[str, int], workshop_id: str, server_count: int) -> dict[str, Any]:
    canonical_name = _canonical_display_name(workshop_id, names)
    if canonical_name and canonical_name not in names:
        names = tuple(sorted((*names, canonical_name), key=lambda value: (value.casefold(), value)))
    display_name = canonical_name or _choose_display_name(name_counts)
    normalized_names = tuple(_dedupe([normalize_mod_text(name) for name in names]))
    compact_names = tuple(_dedupe([compact_mod_text(name) for name in names]))
    token_values = []
    for name in names:
        token_values.extend(_tokenize_mod_name(name))
    return {
        "name": display_name,
        "display_name": display_name,
        "raw_names": names,
        "normalized_names": normalized_names,
        "compact_names": compact_names,
        "normalized_name": normalized_names[0] if normalized_names else "",
        "compact_name": compact_names[0] if compact_names else "",
        "tokens": _dedupe(token_values),
        "server_count": server_count,
        "workshop_id": workshop_id,
    }


def _split_camel_word(word: str) -> tuple[str, ...]:
    if not word:
        return ()

    parts = []
    start = 0
    for idx in range(1, len(word)):
        prev_ch = word[idx - 1]
        ch = word[idx]
        next_ch = word[idx + 1] if idx + 1 < len(word) else ""
        if ch.isupper() and (
            prev_ch.islower()
            or (prev_ch.isupper() and next_ch.islower())
        ):
            parts.append(word[start:idx])
            start = idx
    parts.append(word[start:])
    return tuple(part.lower() for part in parts if part)


def _tokenize_mod_name(name: Any) -> tuple[str, ...]:
    tokens = []
    seen = set()

    def add(token: str) -> None:
        if token and token not in seen:
            tokens.append(token)
            seen.add(token)

    for token in normalize_mod_text(name).split():
        add(token)

    raw_word = []
    for ch in str(name or ""):
        if ch.isalnum():
            raw_word.append(ch)
            continue
        for token in _split_camel_word("".join(raw_word)):
            add(token)
        raw_word = []
    for token in _split_camel_word("".join(raw_word)):
        add(token)

    return tuple(tokens)


def _prefixes(value: str, max_len: int = 32) -> tuple[str, ...]:
    text = str(value or "")
    return tuple(text[:idx] for idx in range(1, min(len(text), max_len) + 1))


def _build_lookup_maps(entries: list[dict[str, Any]]) -> tuple[dict[str, tuple[int, ...]], dict[str, tuple[int, ...]]]:
    prefix_map: dict[str, set[int]] = {}
    exact_compact_map: dict[str, set[int]] = {}

    def add_prefix(value: str, idx: int) -> None:
        for prefix in _prefixes(value):
            prefix_map.setdefault(prefix, set()).add(idx)

    for idx, entry in enumerate(entries):
        for value in entry.get("normalized_names", ()):
            add_prefix(str(value or ""), idx)
        for value in entry.get("compact_names", ()):
            compact = str(value or "")
            add_prefix(compact, idx)
            if compact:
                exact_compact_map.setdefault(compact, set()).add(idx)
        for value in entry.get("tokens", ()):
            add_prefix(str(value or ""), idx)
        workshop_id = str(entry.get("workshop_id") or "")
        add_prefix(workshop_id, idx)
        if workshop_id:
            exact_compact_map.setdefault(workshop_id, set()).add(idx)

    frozen_prefix_map = {key: tuple(sorted(values)) for key, values in prefix_map.items()}
    frozen_exact_map = {key: tuple(sorted(values)) for key, values in exact_compact_map.items()}
    return frozen_prefix_map, frozen_exact_map


def build_mod_suggestion_index(mods_json_values: Iterable[Any]) -> dict[str, Any]:
    mods_by_identity: dict[str, dict[str, Any]] = {}

    for mods_json in mods_json_values or ():
        seen_on_server: set[str] = set()
        seen_names_on_server: set[tuple[str, str]] = set()
        for item in _parse_mods_json(mods_json):
            name = str(item.get("name") or "").strip()
            if not name:
                continue

            workshop_id = _clean_workshop_id(item.get("steamWorkshopId"))
            identity = _mod_identity(name, workshop_id)
            entry = mods_by_identity.get(identity)
            if entry is None:
                mods_by_identity[identity] = {
                    "name_counts": {},
                    "names": set(),
                    "workshop_id": workshop_id,
                    "server_count": 0,
                }
                entry = mods_by_identity[identity]

            if identity not in seen_on_server:
                seen_on_server.add(identity)
                entry["server_count"] += 1
            if workshop_id:
                entry["workshop_id"] = workshop_id

            entry["names"].add(name)
            name_key = (identity, name)
            if name_key not in seen_names_on_server:
                seen_names_on_server.add(name_key)
                entry["name_counts"][name] = int(entry["name_counts"].get(name, 0) or 0) + 1

    if not mods_by_identity:
        return _empty_index()

    entries = [
        _build_entry(
            tuple(sorted(entry.get("names") or (), key=lambda value: (value.casefold(), value))),
            dict(entry.get("name_counts") or {}),
            str(entry.get("workshop_id") or ""),
            int(entry.get("server_count") or 0),
        )
        for entry in mods_by_identity.values()
    ]
    entries.sort(key=lambda entry: (entry["name"].casefold(), entry["workshop_id"]))
    prefix_map, exact_compact_map = _build_lookup_maps(entries)
    return _ModSuggestionIndex({
        "mods": tuple(entries),
        "count": len(entries),
        "cache": {},
        "prefix_map": prefix_map,
        "exact_compact_map": exact_compact_map,
    })


def _target_values(normalized_token: str, compact_token: str) -> tuple[str, ...]:
    values = []
    seen = set()
    for target in SUGGESTION_HINTS.get(compact_token, ()):
        text = str(target or "").strip()
        if text and text not in seen:
            values.append(text)
            seen.add(text)
    for target in alias_values_for(normalized_token, compact_token):
        text = str(target or "").strip()
        if text and text not in seen:
            values.append(text)
            seen.add(text)
    return tuple(values)


def _target_match_quality(
    normalized_token: str,
    compact_token: str,
    entry: dict[str, Any],
    targets: tuple[str, ...],
) -> int | None:
    normalized_names = tuple(str(value or "") for value in entry.get("normalized_names", (entry.get("normalized_name", ""),)))
    compact_names = tuple(str(value or "") for value in entry.get("compact_names", (entry.get("compact_name", ""),)))
    best = None

    for target in targets:
        target_normalized = normalize_mod_text(target)
        target_compact = compact_mod_text(target)
        exact = (
            (target_normalized and any(name == target_normalized for name in normalized_names))
            or (target_compact and any(name == target_compact for name in compact_names))
        )
        prefix = (
            (target_normalized and any(name.startswith(target_normalized) or target_normalized.startswith(name) for name in normalized_names))
            or (target_compact and any(name.startswith(target_compact) or target_compact.startswith(name) for name in compact_names))
        )
        substring = (
            (target_normalized and any(target_normalized in name or name in target_normalized for name in normalized_names))
            or (target_compact and any(target_compact in name or name in target_compact for name in compact_names))
        )
        if exact:
            quality = 0
        elif prefix:
            quality = 2
        elif substring:
            quality = 4
        else:
            continue

        if best is None or quality < best:
            best = quality

    return best


def _alias_boost(normalized_token: str, compact_token: str, entry: dict[str, Any], targets: tuple[str, ...]) -> int:
    if not targets:
        return 0
    if not is_exact_alias(normalized_token, compact_token) and compact_token not in SUGGESTION_HINTS:
        return 0

    normalized_names = tuple(str(value or "") for value in entry.get("normalized_names", (entry.get("normalized_name", ""),)))
    compact_names = tuple(str(value or "") for value in entry.get("compact_names", (entry.get("compact_name", ""),)))
    for target in targets:
        target_normalized = normalize_mod_text(target)
        target_compact = compact_mod_text(target)
        if target_normalized and any(target_normalized in name for name in normalized_names):
            return 1
        if target_compact and any(target_compact in name for name in compact_names):
            return 1

    return 0


def _match_rank(normalized_token: str, compact_token: str, entry: dict[str, Any], targets: tuple[str, ...]) -> int | None:
    normalized_names = tuple(str(value or "") for value in entry.get("normalized_names", (entry.get("normalized_name", ""),)))
    compact_names = tuple(str(value or "") for value in entry.get("compact_names", (entry.get("compact_name", ""),)))
    tokens = tuple(str(token or "") for token in entry.get("tokens", ()))
    workshop_id = str(entry.get("workshop_id") or "")
    target_quality = _target_match_quality(normalized_token, compact_token, entry, targets)
    query_tokens = tuple(token for token in normalized_token.split() if token)

    def all_query_tokens_match_prefix() -> bool:
        if not query_tokens:
            return False
        for query_token in query_tokens:
            compact_query_token = compact_mod_text(query_token)
            if not (
                any(token.startswith(query_token) for token in tokens)
                or any(name.startswith(query_token) for name in normalized_names)
                or any(compact_query_token and compact_name.startswith(compact_query_token) for compact_name in compact_names)
            ):
                return False
        return True

    def all_query_tokens_match_contains() -> bool:
        if not query_tokens:
            return False
        for query_token in query_tokens:
            compact_query_token = compact_mod_text(query_token)
            if not (
                any(query_token in token for token in tokens)
                or any(query_token in name for name in normalized_names)
                or any(compact_query_token and compact_query_token in compact_name for compact_name in compact_names)
            ):
                return False
        return True

    if compact_token and workshop_id and workshop_id == compact_token:
        return 0
    if (
        (normalized_token and any(name == normalized_token for name in normalized_names))
        or (compact_token and any(name == compact_token for name in compact_names))
        or target_quality == 0
    ):
        return 1
    if (
        (normalized_token and any(name.startswith(normalized_token) for name in normalized_names))
        or (compact_token and any(name.startswith(compact_token) for name in compact_names))
        or target_quality == 2
    ):
        return 2
    if compact_token and workshop_id and workshop_id.startswith(compact_token):
        return 2
    if all_query_tokens_match_prefix():
        return 3
    if normalized_token and any(token.startswith(normalized_token) for token in tokens):
        return 3
    if all_query_tokens_match_contains():
        return 4
    if target_quality == 4:
        return 4
    if normalized_token and any(normalized_token in name for name in normalized_names):
        return 4
    if compact_token and any(compact_token in name for name in compact_names):
        return 4
    return None


def suggest_mods(index: Any, token: Any, limit: int = 8) -> tuple[dict[str, Any], ...]:
    normalized_token = normalize_mod_text(token)
    compact_token = compact_mod_text(token)
    if not compact_token or limit <= 0 or not isinstance(index, dict):
        return ()

    cache = index.get("cache")
    if not isinstance(cache, dict):
        cache = {}
        index["cache"] = cache
    cache_key = (normalized_token, compact_token, int(limit))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    if len(cache) > 128:
        cache.clear()

    targets = _target_values(normalized_token, compact_token)
    mods = index.get("mods", ())
    prefix_map = index.get("prefix_map", {})
    exact_compact_map = index.get("exact_compact_map", {})
    if not isinstance(prefix_map, dict):
        prefix_map = {}
    if not isinstance(exact_compact_map, dict):
        exact_compact_map = {}

    candidate_indices = set()

    def add_candidates(key: str) -> None:
        text = str(key or "")
        if not text:
            return
        candidate_indices.update(prefix_map.get(text, ()))
        candidate_indices.update(exact_compact_map.get(text, ()))

    add_candidates(normalized_token)
    add_candidates(compact_token)
    for query_token in normalized_token.split():
        add_candidates(query_token)
        add_candidates(compact_mod_text(query_token))
    for target in targets:
        add_candidates(normalize_mod_text(target))
        add_candidates(compact_mod_text(target))

    if not candidate_indices and len(normalized_token.split()) > 1:
        try:
            candidate_indices.update(range(len(mods)))
        except Exception:
            pass

    if not candidate_indices:
        cache[cache_key] = ()
        return ()

    ranked = []
    for idx in candidate_indices:
        try:
            entry = mods[idx]
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        match_rank = _match_rank(normalized_token, compact_token, entry, targets)
        if match_rank is None:
            continue
        ranked.append(
            (
                match_rank,
                -_alias_boost(normalized_token, compact_token, entry, targets),
                -int(entry.get("server_count") or 0),
                str(entry.get("name") or "").casefold(),
                str(entry.get("workshop_id") or ""),
                repr(entry.get("raw_names", ())),
                entry,
            )
        )

    ranked.sort()
    suggestions = []
    seen_display_names = set()
    seen_workshop_ids = set()
    for item in ranked:
        entry = item[-1]
        workshop_id = str(entry.get("workshop_id") or "")
        display_key = normalize_mod_text(entry.get("display_name") or entry.get("name") or "")
        if workshop_id and workshop_id in seen_workshop_ids:
            continue
        if display_key and display_key in seen_display_names:
            continue
        suggestions.append(entry)
        if workshop_id:
            seen_workshop_ids.add(workshop_id)
        if display_key:
            seen_display_names.add(display_key)
        if len(suggestions) >= limit:
            break
    result = tuple(suggestions)
    cache[cache_key] = result
    return result


def current_comma_token(text: Any, cursor_pos: Any) -> tuple[str, int, int]:
    value = str(text or "")
    try:
        cursor = int(cursor_pos)
    except Exception:
        cursor = len(value)
    cursor = max(0, min(cursor, len(value)))

    segment_start = value.rfind(",", 0, cursor) + 1
    segment_end = value.find(",", cursor)
    if segment_end < 0:
        segment_end = len(value)

    token_start = segment_start
    while token_start < segment_end and value[token_start].isspace():
        token_start += 1

    token_end = segment_end
    while token_end > token_start and value[token_end - 1].isspace():
        token_end -= 1

    return value[token_start:token_end], token_start, token_end


def current_mod_operator_token(text: Any, cursor_pos: Any) -> tuple[str, int, int] | None:
    value = str(text or "")
    try:
        cursor = int(cursor_pos)
    except Exception:
        cursor = len(value)
    cursor = max(0, min(cursor, len(value)))

    match = _MOD_OPERATOR_RE.search(value)
    if not match or cursor < match.end():
        return None

    suffix = value[match.end():]
    suffix_cursor = cursor - match.end()
    token, start, end = current_comma_token(suffix, suffix_cursor)
    return token, match.end() + start, match.end() + end


def replace_comma_token(text: Any, cursor_pos: Any, replacement: Any) -> tuple[str, int]:
    value = str(text or "")
    replacement_text = str(replacement or "").strip()
    _, start, end = current_comma_token(value, cursor_pos)
    updated = f"{value[:start]}{replacement_text}{value[end:]}"
    return updated, start + len(replacement_text)


def replace_mod_operator_token(text: Any, cursor_pos: Any, replacement: Any) -> tuple[str, int]:
    value = str(text or "")
    try:
        cursor = int(cursor_pos)
    except Exception:
        cursor = len(value)
    cursor = max(0, min(cursor, len(value)))

    match = _MOD_OPERATOR_RE.search(value)
    if not match or cursor < match.end():
        return value, cursor

    suffix = value[match.end():]
    new_suffix, suffix_cursor = replace_comma_token(suffix, cursor - match.end(), replacement)
    return f"{value[:match.end()]}{new_suffix}", match.end() + suffix_cursor
