# maps.py

MIN_MAP_COUNT = 5

# Canonical normalisation rules (lowercased keys)
_MAP_NORMALISE = {
    "chernarusplus":        "Chernarus",
    "ChernarusPlus":        "Chernarus",
    "chernarus plus":       "Chernarus",
    "chernarus_plus":       "Chernarus",
    "ChernarusPlusGloom":   "Chernarus",
    "chernarusplusgloom":   "Chernarus",
    "enoch":                "Livonia",
    "ExclusionZonePlus":    "ExclusionZone",
    "exclusionzoneplus":    "ExclusionZone",
    "exclusionzonePlus":    "ExclusionZone",
    "livonia":              "Livonia",
    "deerisle":             "DeerIsle",
    "Deerisle":             "DeerIsle",
    "pnw":                  "PNW",
    "Pnw":                  "PNW",
    "Takistanplus":         "TakistanPlus",
    "takistanplus":         "TakistanPlus",
}

def standardize_map(raw: str) -> str:
    """
    Standardise map names for display + filtering.
    """
    s = (raw or "").strip()
    if not s:
        return ""

    key = s.lower().replace("-", " ").replace("_", " ").strip()
    key = " ".join(key.split())  # squash whitespace

    # direct rules first
    if key in _MAP_NORMALISE:
        return _MAP_NORMALISE[key]

    # also handle "ChernarusPlus" without separators
    key2 = s.lower().replace("_", "").replace("-", "").replace(" ", "")
    if key2 in _MAP_NORMALISE:
        return _MAP_NORMALISE[key2]

    # default: capitalize first char only (keeps original casing mostly)
    return s[0].upper() + s[1:]


def map_choices_from_db_rows(rows: list[dict]) -> list[str]:
    """
    Build dropdown choices from already-loaded DB rows:
    - standardize names
    - include only maps that appear in >= MIN_MAP_COUNT servers
    - "All" always first
    """
    counts: dict[str, int] = {}

    for r in rows or []:
        nm = standardize_map((r.get("map") or "").strip())
        if not nm:
            continue
        counts[nm] = counts.get(nm, 0) + 1

    maps = [m for m, c in counts.items() if c >= MIN_MAP_COUNT]
    maps.sort(key=lambda x: x.lower())

    return ["All"] + maps
