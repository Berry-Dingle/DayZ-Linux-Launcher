#!/usr/bin/env python3
from typing import Any


BUILTIN_MOD_ALIASES = {
    "cf": ("Community Framework", "CF"),
    "df": ("Dabs Framework",),
    "cl": ("CodeLock", "Code Lock"),
    "bbp": ("BaseBuildingPlus", "Base Building Plus"),
    "bf": ("Building Fortifications",),
    "vpp": ("VPPAdminTools",),
    "cot": ("Community-Online-Tools", "Community Online Tools"),
    "vppm": ("VanillaPlusPlusMap",),
    "mmg": ("MMG - Mightys Military Gear", "MMG", "Mightys Military Gear", "MMG Base Storage", "MMG Civilian Clothing"),
    "rag": ("RaG_Core", "RaG_BaseItems", "RaG_BaseBuilding", "RaG_Hunting_Cabin", "RaG_Vehicle_Pack"),
    "snafu": ("SNAFU Weapons", "SNAFU_Weapons"),
    "fog": ("Forward Operator Gear",),
    "rf": ("RedFalcon", "RedFalcon Flight System Heliz", "RedFalcon Watercraft"),
    "sip": ("Server_Information_Panel",),
    "vsm": ("Virtual Storage Module",),
    "kgb": ("KGB.LIB",),
}


def _normalize_alias_text(text: Any) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in str(text or "").lower()).split())


def _compact_alias_text(text: Any) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def alias_values_for(normalized_term: str, compact_term: str) -> tuple[str, ...]:
    matches = []
    seen = set()

    for key, values in BUILTIN_MOD_ALIASES.items():
        key_norm = _normalize_alias_text(key)
        key_compact = _compact_alias_text(key)
        if normalized_term == key_norm or compact_term == key_compact:
            return tuple(str(value or "").strip() for value in values if str(value or "").strip())

        matched = (
            (len(normalized_term) >= 3 and key_norm.startswith(normalized_term))
            or (len(compact_term) >= 3 and key_compact.startswith(compact_term))
            or any(len(normalized_term) >= 3 and _normalize_alias_text(value).startswith(normalized_term) for value in values)
            or any(len(compact_term) >= 3 and _compact_alias_text(value).startswith(compact_term) for value in values)
        )
        if not matched:
            continue

        for value in values:
            text = str(value or "").strip()
            if text and text not in seen:
                matches.append(text)
                seen.add(text)

    return tuple(matches)


def is_exact_alias(normalized_term: Any, compact_term: Any) -> bool:
    normalized = str(normalized_term or "")
    compact = str(compact_term or "")
    for key in BUILTIN_MOD_ALIASES:
        key_norm = _normalize_alias_text(key)
        key_compact = _compact_alias_text(key)
        if normalized == key_norm or compact == key_compact:
            return True
    return False
