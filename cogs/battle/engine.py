"""
cogs/battle/engine.py
────────────────────────
Core battle mechanics: the BattlePokemon model, damage calculation,
secondary effects (stat changes, status ailments), and drain/recoil.
"""

import random
from typing import Optional

from .constants import (
    LEVEL, FALLBACK_MOVE, ABILITY_IMMUNITY,
    HELD_ITEMS, ITEM_ASSIGN_CHANCE, STAT_KEY_MAP, STAT_DISPLAY,
    STATUS_INDUCING_AILMENTS, STATUS_VERB, STATUS_EMOJI, STATUS_TYPE_IMMUNITY,
    SLEEP_MIN_TURNS, SLEEP_MAX_TURNS, _calc_stat, type_multiplier, _stage_multiplier,
)


def type_multiplier_for(move_type: str, defender: "BattlePokemon") -> float:
    """Like type_multiplier(), but folds in ability-based type immunities
    (Levitate/Water Absorb/Volt Absorb/Flash Fire) so the AI's damage
    estimates never rate a move that would actually do nothing."""
    if ABILITY_IMMUNITY.get(getattr(defender, "ability", None)) == move_type:
        return 0.0
    return type_multiplier(move_type, defender.types)


class BattlePokemon:
    def __init__(self, data: dict, moves: list):
        self.name = data["name"]
        self.types = data["types"]
        self.sprite = data.get("sprite")
        s = data.get("stats", {})
        self.max_hp = _calc_stat(s.get("hp", 50), is_hp=True)
        self.hp = self.max_hp
        self.atk = _calc_stat(s.get("attack", 50))
        self.dfn = _calc_stat(s.get("defense", 50))
        self.spa = _calc_stat(s.get("special-attack", 50))
        self.spd = _calc_stat(s.get("special-defense", 50))
        self.spe = _calc_stat(s.get("speed", 50))
        # Raw (pre-truncation) base Speed, kept around purely as a
        # deterministic turn-order tiebreaker — _calc_stat's int()
        # truncation means two Pokemon with different base Speed can land
        # on the exact same computed Level-100 spe, and without this the
        # movers sort would fall straight to random.random() for what
        # looks to a player like "the same matchup", flipping who acts
        # first turn to turn.
        self.base_speed = s.get("speed", 50)
        self.moves = moves or [FALLBACK_MOVE]
        # PP tracking: give each move dict a mutable current_pp field. Each
        # BattlePokemon gets its own freshly-fetched move dicts (get_move_data
        # returns a new dict per call), so mutating these in place here is
        # safe and never bleeds PP usage across different Pokemon/battles.
        for mv in self.moves:
            mv.setdefault("current_pp", mv.get("pp") or 5)
        # Battle-only stat boosts/drops (-6..+6 stages), reset per battle —
        # these are what stat-lowering/raising secondary effects modify.
        self.stat_stages = {"atk": 0, "dfn": 0, "spa": 0, "spd": 0, "spe": 0}
        # Major status condition: None, "burn", "paralysis", "poison",
        # "sleep", or "freeze". status_counter is only meaningful for sleep
        # (counts down the number of turns left asleep).
        self.status: Optional[str] = None
        self.status_counter: int = 0
        # One ability, randomly chosen from this species' non-hidden
        # abilities (if any were fetched/cached). Only a handful of common,
        # high-impact abilities are actually modeled — see KNOWN_ABILITIES.
        abilities = data.get("abilities") or []
        self.ability: Optional[str] = random.choice(abilities) if abilities else None
        # Held item — see HELD_ITEMS. item_used tracks one-shot berries
        # (Oran/Sitrus) so they can only trigger once per battle; Leftovers
        # and Shell Bell aren't consumable so item_used never applies to them.
        self.item: Optional[str] = random.choice(HELD_ITEMS) if random.random() < ITEM_ASSIGN_CHANCE else None
        self.item_used: bool = False

    @property
    def fainted(self) -> bool:
        return self.hp <= 0

    def effective_stat(self, key: str) -> float:
        base = getattr(self, key)
        value = base * _stage_multiplier(self.stat_stages.get(key, 0))
        if key == "atk" and self.status == "burn":
            # Guts turns burn's usual Attack penalty into a bonus instead.
            value *= 1.5 if self.ability == "guts" else 0.5
        elif key == "atk" and self.status is not None and self.ability == "guts":
            value *= 1.5
        if key == "spe" and self.status == "paralysis":
            value *= 0.5
        return value


def calc_damage(attacker: BattlePokemon, defender: BattlePokemon, move: dict):
    power = move.get("power") or 0
    if power <= 0:
        return 0, 1.0, False
    if move.get("damage_class") == "physical":
        a, d = attacker.effective_stat("atk"), defender.effective_stat("dfn")
    else:
        a, d = attacker.effective_stat("spa"), defender.effective_stat("spd")
    stab = 1.5 if move.get("type") in attacker.types else 1.0
    eff = type_multiplier_for(move.get("type", "normal"), defender)
    crit = random.random() < 0.0625
    crit_mult = 1.5 if crit else 1.0
    rand = random.uniform(0.85, 1.0)
    base = (((2 * LEVEL / 5 + 2) * power * a / max(d, 1)) / 50 + 2)
    dmg = int(base * stab * eff * crit_mult * rand)
    return max(dmg, 1), eff, crit


def _apply_secondary_effects(attacker: BattlePokemon, defender: BattlePokemon, move: dict) -> list:
    """Applies a move's secondary stat-change effect (e.g. Acid lowering Sp.
    Def, Superpower lowering the user's own Atk/Def), if any, and returns
    flavor-text lines describing what happened. Not every move has one —
    only moves with a `stat_changes` entry in their PokeAPI data do."""
    stat_changes = move.get("stat_changes") or []
    if not stat_changes:
        return []

    chance = move.get("effect_chance")
    if chance is not None and random.uniform(0, 100) > chance:
        return []  # secondary effect didn't proc this time

    target_self = move.get("target") == "user"
    target = attacker if target_self else defender

    lines = []
    for sc in stat_changes:
        key = STAT_KEY_MAP.get(sc.get("stat"))
        if not key:
            continue  # accuracy/evasion changes aren't modeled
        delta = sc.get("change", 0)
        old = target.stat_stages.get(key, 0)
        new = max(-6, min(6, old + delta))
        target.stat_stages[key] = new
        if new == old:
            continue
        verb = "rose" if delta > 0 else "fell"
        emphasis = "sharply " if abs(delta) >= 2 else ""
        lines.append(f"📉 {target.name.title()}'s {STAT_DISPLAY[key]} {emphasis}{verb}!"
                      if delta < 0 else
                      f"📈 {target.name.title()}'s {STAT_DISPLAY[key]} {emphasis}{verb}!")
    return lines


def _apply_status_ailment(attacker: BattlePokemon, defender: BattlePokemon, move: dict) -> Optional[str]:
    """Applies a move's status ailment (paralysis/sleep/freeze/burn/poison),
    if any, and returns a flavor-text line — or None if the move has no
    modeled ailment, it didn't proc, or it couldn't take effect."""
    ailment = move.get("ailment")
    if ailment not in STATUS_INDUCING_AILMENTS:
        return None

    chance = move.get("ailment_chance") or 0
    if chance and random.uniform(0, 100) > chance:
        return None  # secondary-effect chance didn't proc this time

    target_self = move.get("target") == "user"
    target = attacker if target_self else defender

    if target.status is not None:
        return None  # only one major status at a time
    immune_types = STATUS_TYPE_IMMUNITY.get(ailment, ())
    if isinstance(immune_types, str):
        immune_types = (immune_types,)
    if any(t in target.types for t in immune_types):
        return None

    target.status = ailment
    if ailment == "sleep":
        target.status_counter = random.randint(SLEEP_MIN_TURNS, SLEEP_MAX_TURNS)
    return f"{STATUS_EMOJI[ailment]} {target.name.title()} {STATUS_VERB[ailment]}!"


def _status_precheck(pokemon: BattlePokemon) -> tuple:
    """Called right before a Pokemon would act. Returns (can_move, message).
    Handles sleep/freeze fully preventing the move (with a chance to wake
    up/thaw each turn) and paralysis' chance to flinch-lock the Pokemon in
    place, on top of the passive stat effects handled in effective_stat()."""
    status = pokemon.status
    if status == "sleep":
        pokemon.status_counter -= 1
        if pokemon.status_counter <= 0:
            pokemon.status = None
            return True, f"😴 {pokemon.name.title()} woke up!"
        return False, f"😴 {pokemon.name.title()} is fast asleep."
    if status == "freeze":
        if random.random() < 0.20:
            pokemon.status = None
            return True, f"🧊 {pokemon.name.title()} thawed out!"
        return False, f"🧊 {pokemon.name.title()} is frozen solid!"
    if status == "paralysis":
        if random.random() < 0.25:
            return False, f"⚡ {pokemon.name.title()} is fully paralyzed!"
        return True, None
    return True, None


def _apply_drain_recoil(attacker: BattlePokemon, dmg: int, move: dict) -> Optional[str]:
    """Applies HP-drain (e.g. Giga Drain, positive %) or recoil (e.g. Flare
    Blitz/Double-Edge/Brave Bird, negative %) based on the move's `drain`
    percentage, and returns a flavor-text line, or None if the move has
    neither."""
    drain = move.get("drain") or 0
    if not drain or dmg <= 0:
        return None
    amount = max(1, int(abs(dmg) * abs(drain) / 100))
    if drain > 0:
        healed = min(amount, attacker.max_hp - attacker.hp)
        if healed <= 0:
            return None
        attacker.hp += healed
        return f"🩸 {attacker.name.title()} drained **{healed}** HP!"
    else:
        attacker.hp = max(0, attacker.hp - amount)
        return f"💢 {attacker.name.title()} is hit by recoil! (**{amount}** dmg)"
      
