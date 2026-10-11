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
