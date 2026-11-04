"""Load-local decoded mod data shared by startup's independent consumers."""

import json
from typing import Any

from .mod_metadata import clean_display_mod_name


class ParsedServerMods:
    __slots__ = ("items", "_names")

    def __init__(self, items: list[Any]):
        self.items = items
        self._names: dict[tuple[int, str | None], str] = {}

    @classmethod
    def from_json(cls, mods_json: str) -> "ParsedServerMods":
        if not mods_json:
            return cls([])
        try:
            items = json.loads(mods_json)
        except Exception:
            items = []
        return cls(items if isinstance(items, list) else [])

    def clean_name(self, item: dict[str, Any], workshop_id: str | None) -> str:
        key = (id(item), workshop_id)
        if key not in self._names:
            self._names[key] = clean_display_mod_name(
                item.get("name"), workshop_id, fallback=False,
            )
        return self._names[key]


def parse_mods_preview(
    mods_json: str, max_names: int = 8, *, parsed_mods: ParsedServerMods | None = None,
) -> tuple[int, str]:
    """
    Returns:
      (total_mod_count, preview_string)

    preview_string:
      - up to max_names mod names
      - adds ", ..." if truncated
    """
    if parsed_mods is None and not mods_json:
        return 0, ""
    try:
        arr = parsed_mods.items if parsed_mods is not None else json.loads(mods_json)
        if not isinstance(arr, list):
            return 0, ""

        names = []
        for it in arr:
            if isinstance(it, dict):
                sid = str(it.get("steamWorkshopId") or "").strip()
                name_id = sid if sid.isdigit() else None
                nm = (parsed_mods.clean_name(it, name_id) if parsed_mods is not None else
                      clean_display_mod_name(it.get("name"), name_id, fallback=False))
                if nm:
                    names.append(nm)

        total = len(names)
        if total == 0:
            return 0, ""

        preview = ", ".join(names[:max_names])
        if total > max_names:
            preview += ", ..."
        return total, preview
    except Exception:
        return 0, ""
