#!/usr/bin/env python3
import json
import re
from typing import Any

from .mod_alias import alias_values_for, is_exact_alias


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SPACE_RE = re.compile(r"\s+")
_MOD_OPERATOR_RE = re.compile(r"(^|\s)(mods:|-mods=)", re.IGNORECASE)
_VALID_SHORT_TEXT_ALIASES = frozenset(("bf", "cf", "cl", "df", "rf"))


def normalize_mod_text(text: Any) -> str:
    text = str(text or "").strip().lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def compact_mod_text(text: Any) -> str:
    return _NON_ALNUM_RE.sub("", str(text or "").strip().lower())


def _dedupe(values: list[str]) -> tuple[str, ...]:
    out = []
    seen = set()
    for value in values:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return tuple(out)


def _empty_index() -> dict[str, frozenset[str]]:
    empty = frozenset()
    return {"ids": empty, "names": empty, "normalized_names": empty, "compact_names": empty, "tokens": empty}


def _term_candidates(term: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    normalized_term = normalize_mod_text(term)
    compact_term = compact_mod_text(term)
    alias_targets = alias_values_for(normalized_term, compact_term)
    candidates = list(alias_targets) if is_exact_alias(normalized_term, compact_term) and alias_targets else [term]
    if not (is_exact_alias(normalized_term, compact_term) and alias_targets):
        candidates.extend(alias_targets)

    normalized = []
    compact = []
    tokens = []
    for value in candidates:
        norm_value = normalize_mod_text(value)
        compact_value = compact_mod_text(value)
        if norm_value:
            normalized.append(norm_value)
            tokens.extend(norm_value.split())
        if compact_value:
            compact.append(compact_value)

    return _dedupe(normalized), _dedupe(compact), _dedupe(tokens)


def _parse_mod_terms(raw_terms: Any) -> tuple[tuple[str, str, str, bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...]:
    raw = str(raw_terms or "")
    parsed = []
    for term in raw.split(","):
        term = term.strip()
        if not term:
            continue
        normalized_term = normalize_mod_text(term)
        compact_term = compact_mod_text(term)
        if not compact_term:
            continue
        exact_alias = is_exact_alias(normalized_term, compact_term)
        if not compact_term.isdigit() and len(compact_term) < 3 and compact_term not in _VALID_SHORT_TEXT_ALIASES:
            parsed.append((term, normalized_term, compact_term, True, (), (), ()))
            continue

        normalized_candidates, compact_candidates, token_candidates = _term_candidates(term)
        if exact_alias:
            token_candidates = ()
        parsed.append((term, normalized_term, compact_term, exact_alias, normalized_candidates, compact_candidates, token_candidates))
    return tuple(parsed)


def parse_mod_query(raw_query: Any) -> tuple[tuple[str, str, str, bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...]:
    raw = str(raw_query or "")
    if "," not in raw:
        return ()
    return _parse_mod_terms(raw)


def parse_required_mod_query(raw_terms: Any) -> tuple[tuple[str, str, str, bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...]:
    return _parse_mod_terms(raw_terms)


def split_mod_search_operator(raw_query: Any) -> tuple[str, str, bool]:
    raw = str(raw_query or "")
    match = _MOD_OPERATOR_RE.search(raw)
    if not match:
        return raw.strip(), "", False
    return raw[:match.start()].strip(), raw[match.end():].strip(), True


def build_server_mod_index(mods_json: Any) -> dict[str, frozenset[str]]:
    ids: set[str] = set()
    normalized_names: set[str] = set()
    compact_names: set[str] = set()
    tokens: set[str] = set()

    if not mods_json:
        return _empty_index()

    try:
        arr = json.loads(str(mods_json))
    except Exception:
        return _empty_index()

    if not isinstance(arr, list):
        return _empty_index()

    for item in arr:
        if not isinstance(item, dict):
            continue

        sid = item.get("steamWorkshopId")
        if sid is not None:
            sid_text = str(sid).strip()
            if sid_text.isdigit():
                ids.add(sid_text)

        name = item.get("name")
        normalized_name = normalize_mod_text(name)
        compact_name = compact_mod_text(name)
        if normalized_name:
            normalized_names.add(normalized_name)
            tokens.update(normalized_name.split())
        if compact_name:
            compact_names.add(compact_name)

    frozen_compact_names = frozenset(compact_names)
    return {
        "ids": frozenset(ids),
        "names": frozen_compact_names,
        "normalized_names": frozenset(normalized_names),
        "compact_names": frozen_compact_names,
        "tokens": frozenset(tokens),
    }


def _matches_text_term(
    normalized_term: str,
    compact_term: str,
    normalized_candidates: tuple[str, ...],
    compact_candidates: tuple[str, ...],
    token_candidates: tuple[str, ...],
    normalized_names: frozenset[str],
    compact_names: frozenset[str],
    tokens: frozenset[str],
    allow_raw_fallback: bool = True,
) -> bool:
    for candidate in normalized_candidates:
        if any(candidate in name for name in normalized_names):
            return True

    for candidate in compact_candidates:
        if len(candidate) >= 2 and any(candidate in name for name in compact_names):
            return True

    for candidate in token_candidates:
        if len(candidate) >= 2 and any(token.startswith(candidate) for token in tokens):
            return True

    if allow_raw_fallback:
        if normalized_term and any(normalized_term in name for name in normalized_names):
            return True
        if len(compact_term) >= 2 and any(compact_term in name for name in compact_names):
            return True
        if len(normalized_term) >= 2 and any(token.startswith(normalized_term) for token in tokens):
            return True

    return False


def server_matches_mod_query(mod_index: Any, parsed_query: Any) -> bool:
    if not parsed_query:
        return True
    if not isinstance(mod_index, dict):
        return False

    ids = mod_index.get("ids", frozenset())
    normalized_names = mod_index.get("normalized_names", frozenset())
    compact_names = mod_index.get("compact_names", mod_index.get("names", frozenset()))
    tokens = mod_index.get("tokens", frozenset())

    for term, normalized_term, compact_term, exact_alias, normalized_candidates, compact_candidates, token_candidates in parsed_query:
        if compact_term.isdigit():
            if compact_term not in ids:
                return False
            continue

        if not _matches_text_term(
            normalized_term,
            compact_term,
            normalized_candidates,
            compact_candidates,
            token_candidates,
            normalized_names,
            compact_names,
            tokens,
            allow_raw_fallback=not bool(exact_alias),
        ):
            return False

    return True
