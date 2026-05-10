"""
pokedata.py  —  Load and query the Pokémon type / region CSV.

Expected CSV columns:  dex_number, name, region, type1, type2
"""

import csv
import os
from pathlib import Path

# Path relative to this file
_CSV_PATH     = Path(__file__).parent / "data" / "typeandregion.csv"
_CDN_CSV_PATH = Path(__file__).parent / "data" / "pokemon_cdn_mapping.csv"

# name (lowercase) → {"name": str, "region": str, "type1": str, "type2": str|None}
_POKEMON_DATA: dict[str, dict] = {}

# name (lowercase) → cdn_number (int)
_CDN_MAPPING: dict[str, int] = {}


def load() -> None:
    """Load all CSVs into memory. Call once at startup."""
    global _POKEMON_DATA, _CDN_MAPPING

    # Type / region data
    if not _CSV_PATH.exists():
        print(f"[pokedata] WARNING: {_CSV_PATH} not found — type/region stats unavailable")
    else:
        _POKEMON_DATA = {}
        with open(_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name  = row["name"].strip()
                key   = name.lower()
                type2 = row.get("type2", "").strip() or None
                _POKEMON_DATA[key] = {
                    "name":   name,
                    "region": row.get("region", "Unknown").strip(),
                    "type1":  row.get("type1", "Unknown").strip(),
                    "type2":  type2,
                }
        print(f"[pokedata] Loaded {len(_POKEMON_DATA)} Pokémon entries")

    # CDN image mapping
    if not _CDN_CSV_PATH.exists():
        print(f"[pokedata] WARNING: {_CDN_CSV_PATH} not found — CDN image URLs unavailable")
    else:
        _CDN_MAPPING = {}
        with open(_CDN_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                _CDN_MAPPING[row["name"].strip().lower()] = int(row["cdn_number"].strip())
        print(f"[pokedata] Loaded {len(_CDN_MAPPING)} CDN mapping entries")


def get(pokemon_name: str) -> dict | None:
    return _POKEMON_DATA.get(pokemon_name.lower())


def get_region(pokemon_name: str) -> str:
    d = get(pokemon_name)
    return d["region"] if d else "Unknown"


def get_types(pokemon_name: str) -> list[str]:
    d = get(pokemon_name)
    if not d:
        return []
    return [t for t in (d["type1"], d["type2"]) if t]


def aggregate_types(pokemon_counts: list[dict]) -> dict[str, int]:
    """
    Given [{pokemon, count}, ...] return {type_name: total_count}.
    A dual-type Pokémon contributes its count to both types.
    """
    totals: dict[str, int] = {}
    for entry in pokemon_counts:
        for t in get_types(entry["pokemon"]):
            totals[t] = totals.get(t, 0) + entry["count"]
    return dict(sorted(totals.items(), key=lambda x: x[1], reverse=True))


def aggregate_regions(pokemon_counts: list[dict]) -> dict[str, int]:
    """Given [{pokemon, count}, ...] return {region: total_count}."""
    totals: dict[str, int] = {}
    for entry in pokemon_counts:
        region = get_region(entry["pokemon"])
        totals[region] = totals.get(region, 0) + entry["count"]
    return dict(sorted(totals.items(), key=lambda x: x[1], reverse=True))


def cdn_image_url(pokemon_name: str) -> str | None:
    """Return the CDN image URL for a Pokémon, or None if not in mapping."""
    num = _CDN_MAPPING.get(pokemon_name.lower())
    if num is None:
        return None
    return f"https://cdn.poketwo.net/images/{num}.png"
