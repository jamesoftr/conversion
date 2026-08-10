"""
cogs/battle/constants.py
─────────────────────────
Shared constants, small pure-math helpers, and the Mongo collection
accessor used across the battle package. No Discord/PokeAPI I/O lives
here — just data every other battle module needs.
"""

import sys
from typing import Optional

try:
    import PIL  # noqa: F401 — presence check only; render.py imports the actual names
    PIL_OK = True
except ImportError:
    PIL_OK = False
    print("[battle_cog] Pillow not installed — battle scene images disabled, "
          "falling back to text embeds.", file=sys.stderr)

import db as _db


def _col():
    return _db.get_db()

POKEAPI = "https://pokeapi.co/api/v2"
LEVEL = 100
TURN_TIMEOUT = 90  # seconds each turn's panel stays open
AFK_FORFEIT_STRIKES = 2  # consecutive missed turns before a trainer auto-forfeits

# ── Type effectiveness chart (attacking type -> {defending type: multiplier}) ──
# Only non-1.0 entries listed; anything missing defaults to 1.0.
TYPE_CHART = {
    "normal":   {"rock": 0.5, "ghost": 0.0, "steel": 0.5},
    "fire":     {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 2.0, "bug": 2.0,
                 "rock": 0.5, "dragon": 0.5, "steel": 2.0},
    "water":    {"fire": 2.0, "water": 0.5, "grass": 0.5, "ground": 2.0,
                 "rock": 2.0, "dragon": 0.5},
    "electric": {"water": 2.0, "electric": 0.5, "grass": 0.5, "ground": 0.0,
                 "flying": 2.0, "dragon": 0.5},
    "grass":    {"fire": 0.5, "water": 2.0, "grass": 0.5, "poison": 0.5,
                 "ground": 2.0, "flying": 0.5, "bug": 0.5, "rock": 2.0,
                 "dragon": 0.5, "steel": 0.5},
    "ice":      {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 0.5,
                 "ground": 2.0, "flying": 2.0, "dragon": 2.0, "steel": 0.5},
    "fighting": {"normal": 2.0, "ice": 2.0, "poison": 0.5, "flying": 0.5,
                 "psychic": 0.5, "bug": 0.5, "rock": 2.0, "ghost": 0.0,
                 "dark": 2.0, "steel": 2.0, "fairy": 0.5},
    "poison":   {"grass": 2.0, "poison": 0.5, "ground": 0.5, "rock": 0.5,
                 "ghost": 0.5, "steel": 0.0, "fairy": 2.0},
    "ground":   {"fire": 2.0, "electric": 2.0, "grass": 0.5, "poison": 2.0,
                 "flying": 0.0, "bug": 0.5, "rock": 2.0, "steel": 2.0},
    "flying":   {"electric": 0.5, "grass": 2.0, "fighting": 2.0, "bug": 2.0,
                 "rock": 0.5, "steel": 0.5},
    "psychic":  {"fighting": 2.0, "poison": 2.0, "psychic": 0.5, "dark": 0.0,
                 "steel": 0.5},
    "bug":      {"fire": 0.5, "grass": 2.0, "fighting": 0.5, "poison": 0.5,
                 "flying": 0.5, "psychic": 2.0, "ghost": 0.5, "dark": 2.0,
                 "steel": 0.5, "fairy": 0.5},
    "rock":     {"fire": 2.0, "ice": 2.0, "fighting": 0.5, "ground": 0.5,
                 "flying": 2.0, "bug": 2.0, "steel": 0.5},
    "ghost":    {"normal": 0.0, "psychic": 2.0, "ghost": 2.0, "dark": 0.5},
    "dragon":   {"dragon": 2.0, "steel": 0.5, "fairy": 0.0},
    "dark":     {"fighting": 0.5, "psychic": 2.0, "ghost": 2.0, "dark": 0.5,
                 "fairy": 0.5},
    "steel":    {"fire": 0.5, "water": 0.5, "electric": 0.5, "ice": 2.0,
                 "rock": 2.0, "steel": 0.5, "fairy": 2.0},
    "fairy":    {"fire": 0.5, "fighting": 2.0, "poison": 0.5, "dragon": 2.0,
                 "dark": 2.0, "steel": 0.5},
}

FALLBACK_MOVE = {
    "name": "struggle", "power": 50, "accuracy": 100, "pp": 1,
    "type": "normal", "damage_class": "physical", "priority": 0,
    "effect_chance": None, "stat_changes": [], "drain": 0,
    "target": "selected-pokemon", "ailment": None, "ailment_chance": 0,
}
STRUGGLE_RECOIL_FRACTION = 0.25  # Struggle's recoil is 1/4 of the USER's max HP, not damage-based

# ── Battle math ──────────────────────────────────────────────────────────────

def _calc_stat(base: int, is_hp: bool = False, level: int = LEVEL) -> int:
    if is_hp:
        return int(((2 * base + 31) * level) / 100) + level + 10
    return int(((2 * base + 31) * level) / 100) + 5

def type_multiplier(move_type: str, defender_types: list) -> float:
    mult = 1.0
    for t in defender_types:
        mult *= TYPE_CHART.get(move_type, {}).get(t, 1.0)
    return mult

# ── Abilities ────────────────────────────────────────────────────────────────
# Only a handful of common, mechanically-simple abilities are modeled — held
# items, weather, and the full ability roster are deliberately out of scope.
KNOWN_ABILITIES = {
    "levitate", "water-absorb", "volt-absorb", "flash-fire",
    "intimidate", "guts", "sturdy",
}
# Move-type -> the ability that grants full immunity to it.
ABILITY_IMMUNITY = {
    "ground": "levitate",
    "water": "water-absorb",
    "electric": "volt-absorb",
    "fire": "flash-fire",
}
# Of those immunities, which ones heal the holder instead of just no-selling.
ABILITY_ABSORB_HEAL = {"water-absorb", "volt-absorb"}

# ── Held items ──────────────────────────────────────────────────────────────
# Deliberately "simple"/sustain-only — no damage boosters, crit boosters, or
# OHKO-survival items (Focus Sash/Band) since those swing damage output
# directly and would be too strong given the rest of the engine doesn't
# model item counterplay. Each Pokemon has a chance to be holding one.
HELD_ITEMS = ["leftovers", "oran-berry", "sitrus-berry", "shell-bell"]
ITEM_ASSIGN_CHANCE = 0.45

def item_label(item: Optional[str]) -> str:
    return item.replace("-", " ").title() if item else ""

# Maps PokeAPI stat names -> the short attribute names BattlePokemon uses.
# (accuracy/evasion aren't modeled as separate battle stats here, so
# stat-changing effects that target those are simply ignored.)
STAT_KEY_MAP = {
    "attack": "atk", "defense": "dfn",
    "special-attack": "spa", "special-defense": "spd",
    "speed": "spe",
}
STAT_DISPLAY = {"atk": "Attack", "dfn": "Defense", "spa": "Sp. Atk",
                "spd": "Sp. Def", "spe": "Speed"}

SELF_KO_MOVES = {"explosion", "self-destruct"}

# Moves that force the user to spend the following turn recharging — no
# move, no switching — regardless of whether the move hit or missed.
RECHARGE_MOVES = {
    "hyper-beam", "giga-impact", "hydro-cannon", "frenzy-plant",
    "blast-burn", "rock-wrecker", "roar-of-time", "prismatic-laser",
    "meteor-assault", "eternabeam",
}

def _stage_multiplier(stage: int) -> float:
    stage = max(-6, min(6, stage))
    if stage >= 0:
        return (2 + stage) / 2
    return 2 / (2 - stage)

# ── Move-selection tuning ───────────────────────────────────────────────────
PRIORITY_MOVE_MIN_POWER = 40  # e.g. Quick Attack/Aqua Jet/Mach Punch-tier or better
STAB_MOVE_MIN_POWER = 70  # guarantee a slot for own-type moves above this power
STATUS_INDUCING_AILMENTS = {"paralysis", "sleep", "freeze", "burn", "poison"}

# ── Status conditions ───────────────────────────────────────────────────────
STATUS_VERB = {
    "burn": "was burned", "paralysis": "was paralyzed", "poison": "was poisoned",
    "sleep": "fell asleep", "freeze": "was frozen solid",
}
STATUS_EMOJI = {"burn": "🔥", "paralysis": "⚡", "poison": "☠️", "sleep": "😴", "freeze": "🧊"}
# Type-based immunities to specific status conditions (a small, cheap-to-add
# nicety that matches the mainline games and stops e.g. Electric-types ever
# getting paralyzed by a Body Slam).
STATUS_TYPE_IMMUNITY = {
    "burn": "fire", "paralysis": "electric", "freeze": "ice",
    "poison": ("poison", "steel"),
}
SLEEP_MIN_TURNS, SLEEP_MAX_TURNS = 1, 3

# ── Battle scene image canvas ───────────────────────────────────────────────
CANVAS_W, CANVAS_H = 720, 380
SPRITE_SCALE = 3
