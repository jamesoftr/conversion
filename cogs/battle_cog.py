"""
cogs/battle_cog.py
───────────────────
Pokemon battling cog.

Data
----
Pokemon + move data is pulled live from PokeAPI (https://pokeapi.co) the
first time a given Pokemon/move is needed, then cached in MongoDB
(collections `pokedex_cache` and `move_cache`) so repeat battles don't
re-hit the API. Uses the same `db.get_db()` pattern as welcome_cog.py.

Commands (prefix, assumes bot already has a command_prefix like "!")
----------------------------------------------------------------------
!battle @user [format] [count] [>min<max]
    format : "random" (default) or "custom"
    count  : 1-6 pokemon per side (default 3)
    >min<max : optional, "random" format only — restricts every rolled
               Pokemon to one whose base stat total (BST, i.e. the sum of
               its 6 base stats) falls in this range. Either bound can be
               omitted: ">550" means BST >= 550, "<700" means BST <= 700,
               ">590<700" means both. E.g. `!battle @rival random 3 >590<700`
               only rolls Pokemon roughly in the legendary/pseudo-legendary
               BST range.
    Posts a challenge with Accept / Decline buttons for the opponent.
    - random  → both teams are auto-rolled and the battle starts immediately
                on accept.
    - custom  → both trainers then build their own team with `!battle add`.

!battle @<bot's name> random [count] [>min<max]
    Battle the bot itself instead of another user — no Accept/Decline step,
    the battle starts immediately. The bot plays its own team with an AI
    that calculates the expected damage of every available move against the
    opponent's current active Pokemon — factoring in STAB, type
    effectiveness, and effective stats — and attacks with whichever move
    hits hardest; on a forced switch it sends out its next healthy Pokemon
    automatically. Only "random" format is supported against the bot.

!battle add <name>, <name>, ...
    Add Pokemon (comma-separated, case-insensitive) to your team while a
    "custom" challenge is pending in the channel. Can be called multiple
    times. Once BOTH trainers have submitted `count` pokemon, the battle
    starts automatically.

!battle cancel
    Cancels a pending challenge/team-build, or force-ends an active battle
    in the current channel.

Battle flow / UI
-----------------
All Pokemon battle at LEVEL 100. Each turn (after the first) is posted as
two separate messages, paced 3 seconds apart:

    1. A plain text-only embed recapping the previous turn's results
       (damage dealt, switches, etc.) — skipped on turn 1, since there's
       nothing to recap yet.
    2. The actionable panel: a battle-scene image (both Pokemon with an
       HP bar above their sprite) alongside each trainer's current
       Pokemon/HP and a single view containing:

    • a move dropdown for trainer 1
    • a move dropdown for trainer 2
    • a shared "🔄 Switch" button
    • a shared "⏭️ Pass Turn" button

Both dropdowns/buttons live on the same message (so the whole channel can
watch the battle), but each component checks `interaction.user` before
acting — a trainer can only ever submit their OWN action. Clicking Switch
opens a private (ephemeral) menu of your remaining team that only you can
see, so your bench isn't spoiled for your opponent while you're deciding.

Each trainer locks in exactly ONE action per turn: a move, a switch, or a
pass. Switching *consumes the whole turn* — a Pokemon that switches in
never also attacks that same turn. This is a deliberate fix for a bug in
the previous version where a switched-in Pokemon could end up executing a
move nobody selected; because switch and move are now mutually exclusive
per-turn actions resolved from a single locked-in dict (instead of two
separate sequential prompts), there's no code path left that can attack
with a Pokemon the trainer didn't choose to attack with. If a trainer
doesn't respond before the turn timer runs out, they no longer get a
*random* move — they auto-use their strongest available move instead
(moves are pre-sorted by power), and the panel says so.

How the 4 moves are chosen
---------------------------
`pick_moves()` fetches every damage-dealing (non-status) move in a
Pokemon's learnable move pool, sorts them by base power, and keeps the
top 4. So each Pokemon always has its four hardest-hitting moves
available — not a random sample.
"""

import asyncio
import io
import random
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
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
    "target": "selected-pokemon",
}


# ── PokeAPI fetch + Mongo cache ─────────────────────────────────────────────

async def get_pokemon_data(session: aiohttp.ClientSession, ident: str) -> Optional[dict]:
    key = str(ident).lower().strip().replace(" ", "-")
    doc = await _col().pokedex_cache.find_one({"_id": key})
    if doc:
        return doc
    try:
        async with session.get(f"{POKEAPI}/pokemon/{key}", timeout=10) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    stats = {s["stat"]["name"]: s["base_stat"] for s in data["stats"]}
    types = [t["type"]["name"] for t in data["types"]]
    move_pool = [m["move"]["name"] for m in data["moves"]]
    sprite = (data.get("sprites") or {}).get("front_default") or ""

    doc = {
        "_id": key,
        "name": data["name"],
        "dex_id": data["id"],
        "types": types,
        "stats": stats,
        "move_pool": move_pool,
        "sprite": sprite,
    }
    await _col().pokedex_cache.update_one({"_id": key}, {"$set": doc}, upsert=True)
    return doc


async def get_move_data(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    key = name.lower().strip()
    doc = await _col().move_cache.find_one({"_id": key})
    if doc:
        return doc
    try:
        async with session.get(f"{POKEAPI}/move/{key}", timeout=10) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    doc = {
        "_id": key,
        "name": data["name"],
        "power": data.get("power"),
        "accuracy": data.get("accuracy"),
        "pp": data.get("pp"),
        "type": data["type"]["name"],
        "damage_class": data["damage_class"]["name"],
        "priority": data.get("priority", 0),
        # Secondary-effect data, used to apply stat drops/boosts and
        # recoil/drain on top of raw damage.
        "effect_chance": data.get("effect_chance"),
        "stat_changes": [
            {"stat": sc["stat"]["name"], "change": sc["change"]}
            for sc in (data.get("stat_changes") or [])
        ],
        "drain": (data.get("meta") or {}).get("drain", 0),
        "target": (data.get("target") or {}).get("name", "selected-pokemon"),
    }
    await _col().move_cache.update_one({"_id": key}, {"$set": doc}, upsert=True)
    return doc


PRIORITY_MOVE_MIN_POWER = 40  # e.g. Quick Attack/Aqua Jet/Mach Punch-tier or better
STAB_MOVE_MIN_POWER = 70  # guarantee a slot for own-type moves above this power


async def pick_moves(session: aiohttp.ClientSession, data: dict, count: int = 4) -> list:
    """Return `count` damage-dealing moves for this Pokemon (status moves
    excluded), deterministically — never a random sample.

    Selection favors *type coverage* over pure power: at most one move per
    elemental type is picked while a still-unused type has a candidate
    available, so a Pokemon's moveset isn't four same-type moves when it
    knows better than that. Only once every learnable type has already
    been used does it fall back to a second (or third...) move of a type
    already picked — so a genuinely mono-type-movepool Pokemon still ends
    up with a full set instead of empty slots.

    On top of that, if the Pokemon can learn a decent-power priority move
    (priority > 0, power >= PRIORITY_MOVE_MIN_POWER — e.g. Quick Attack,
    Aqua Jet, Mach Punch, Extreme Speed, Sucker Punch), the single best one
    of those is guaranteed a slot even if it wouldn't otherwise crack the
    top picks by power/coverage alone. It also counts toward the
    type-coverage pass, so it won't get displaced by a same-type duplicate.

    Same idea for STAB: for each of the Pokemon's own types, if it can
    learn a move of that type with power > STAB_MOVE_MIN_POWER, the
    strongest such move is guaranteed a slot (e.g. a Water/Psychic Pokemon
    gets its best >70-power Water move and best >70-power Psychic move
    locked in, ahead of coverage moves of types it isn't even STAB on).
    """
    pool = data.get("move_pool", [])
    if not pool:
        tackle = await get_move_data(session, "tackle")
        return [tackle] if tackle else [FALLBACK_MOVE]

    results = await asyncio.gather(
        *[get_move_data(session, name) for name in pool],
        return_exceptions=True,
    )
    candidates = [
        mv for mv in results
        if isinstance(mv, dict) and mv.get("power") and mv.get("damage_class") != "status"
    ]
    if not candidates:
        tackle = await get_move_data(session, "tackle")
        return [tackle] if tackle else [FALLBACK_MOVE]

    candidates.sort(key=lambda m: m.get("power") or 0, reverse=True)

    chosen = []
    used_types = set()

    priority_candidates = [
        m for m in candidates
        if m.get("priority", 0) > 0 and (m.get("power") or 0) >= PRIORITY_MOVE_MIN_POWER
    ]
    if priority_candidates:
        best_priority = max(priority_candidates, key=lambda m: m.get("power") or 0)
        chosen.append(best_priority)
        used_types.add(best_priority.get("type"))

    # STAB guarantee: for each of the Pokemon's own types, lock in its
    # strongest move of that type if it's above the power threshold —
    # ahead of the general coverage pass, so a Water/Psychic Pokemon's
    # best Water and Psychic moves aren't crowded out by, say, a
    # higher-power but off-type coverage move.
    for ptype in data.get("types", []):
        if len(chosen) >= count:
            break
        stab_candidates = [
            m for m in candidates
            if m.get("type") == ptype and (m.get("power") or 0) > STAB_MOVE_MIN_POWER
            and m not in chosen
        ]
        if not stab_candidates:
            continue
        best_stab = max(stab_candidates, key=lambda m: m.get("power") or 0)
        chosen.append(best_stab)
        used_types.add(ptype)

    # Pass 1: strongest move of each not-yet-used type, for coverage.
    for mv in candidates:
        if len(chosen) >= count:
            break
        if mv in chosen:
            continue
        mtype = mv.get("type")
        if mtype in used_types:
            continue
        chosen.append(mv)
        used_types.add(mtype)

    # Pass 2: ran out of distinct types before filling every slot — top up
    # with the next-strongest remaining moves regardless of type.
    for mv in candidates:
        if len(chosen) >= count:
            break
        if mv in chosen:
            continue
        chosen.append(mv)

    return chosen[:count]


# ── Base stat total (BST) filtering for `random` battles ───────────────────
# "BST" here = sum of a Pokemon's 6 base stats (hp/atk/dfn/spa/spd/spe), the
# same number people mean when they say e.g. "Dragonite has 600 BST". This
# has nothing to do with IVs — the cog doesn't model IVs at all; every
# Pokemon battles with the same fixed stat calc (see _calc_stat).

BST_FILTER_RE = re.compile(r'([<>])\s*=?\s*(\d+)')
BST_MAX_ROLL_ATTEMPTS = 120  # random dex-id rolls to try before giving up


def parse_bst_filter(raw: Optional[str]):
    """Parse a trailing threshold string like '>550', '<700', or
    '>590<700' into (min_total, max_total). Either or both may end up
    None. Returns (None, None) for a falsy/unrecognized input."""
    if not raw:
        return None, None
    min_total = max_total = None
    for sign, num in BST_FILTER_RE.findall(raw):
        if sign == ">":
            min_total = int(num)
        else:
            max_total = int(num)
    return min_total, max_total


def format_bst_filter(min_total: Optional[int], max_total: Optional[int]) -> str:
    if min_total is None and max_total is None:
        return ""
    lo = min_total if min_total is not None else 0
    hi = max_total if max_total is not None else "∞"
    return f" (BST filter: {lo}–{hi})"


def _bst(stats: dict) -> int:
    return sum(stats.values())


async def _find_cached_bst_match(min_total: Optional[int], max_total: Optional[int]) -> Optional[dict]:
    """Try to grab a random already-cached Pokemon whose BST is in range,
    via a Mongo aggregation, so repeat rolls with the same/overlapping
    filter get fast once the cache has warmed up. Returns None (falls
    back to rolling PokeAPI dex ids) if nothing matches yet."""
    conditions = {}
    if min_total is not None:
        conditions["$gte"] = min_total
    if max_total is not None:
        conditions["$lte"] = max_total
    if not conditions:
        return None
    pipeline = [
        {"$addFields": {"_bst": {"$sum": {"$map": {
            "input": {"$objectToArray": "$stats"}, "as": "s", "in": "$$s.v"
        }}}}},
        {"$match": {"_bst": conditions}},
        {"$sample": {"size": 1}},
    ]
    async for doc in _col().pokedex_cache.aggregate(pipeline):
        return doc
    return None


async def build_random_pokemon(session: aiohttp.ClientSession,
                                min_total: Optional[int] = None,
                                max_total: Optional[int] = None) -> "BattlePokemon":
    if min_total is not None or max_total is not None:
        cached = await _find_cached_bst_match(min_total, max_total)
        if cached:
            moves = await pick_moves(session, cached)
            return BattlePokemon(cached, moves)

        # Nothing cached matches yet — roll random dex ids and check each
        # one's BST, caching every fetch as we go (get_pokemon_data does
        # this automatically) so future rolls find matches instantly via
        # the aggregation above.
        last_valid = None
        for _ in range(BST_MAX_ROLL_ATTEMPTS):
            dex_id = random.randint(1, 1025)
            data = await get_pokemon_data(session, str(dex_id))
            if not data:
                continue
            last_valid = data
            total = _bst(data.get("stats", {}))
            if min_total is not None and total < min_total:
                continue
            if max_total is not None and total > max_total:
                continue
            moves = await pick_moves(session, data)
            return BattlePokemon(data, moves)

        # Gave up after BST_MAX_ROLL_ATTEMPTS tries (filter is too narrow
        # for what's been rolled) — fall back to the last valid Pokemon
        # seen rather than erroring out mid battle-build.
        data = last_valid or await get_pokemon_data(session, "pikachu")
        moves = await pick_moves(session, data)
        return BattlePokemon(data, moves)

    for _ in range(6):
        dex_id = random.randint(1, 1025)
        data = await get_pokemon_data(session, str(dex_id))
        if data:
            moves = await pick_moves(session, data)
            return BattlePokemon(data, moves)
    data = await get_pokemon_data(session, "pikachu")
    moves = await pick_moves(session, data)
    return BattlePokemon(data, moves)


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


def _stage_multiplier(stage: int) -> float:
    stage = max(-6, min(6, stage))
    if stage >= 0:
        return (2 + stage) / 2
    return 2 / (2 - stage)


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
        # Battle-only stat boosts/drops (-6..+6 stages), reset per battle —
        # these are what stat-lowering/raising secondary effects modify.
        self.stat_stages = {"atk": 0, "dfn": 0, "spa": 0, "spd": 0, "spe": 0}

    @property
    def fainted(self) -> bool:
        return self.hp <= 0

    def effective_stat(self, key: str) -> float:
        base = getattr(self, key)
        return base * _stage_multiplier(self.stat_stages.get(key, 0))


def calc_damage(attacker: BattlePokemon, defender: BattlePokemon, move: dict):
    power = move.get("power") or 0
    if power <= 0:
        return 0, 1.0, False
    if move.get("damage_class") == "physical":
        a, d = attacker.effective_stat("atk"), defender.effective_stat("dfn")
    else:
        a, d = attacker.effective_stat("spa"), defender.effective_stat("spd")
    stab = 1.5 if move.get("type") in attacker.types else 1.0
    eff = type_multiplier(move.get("type", "normal"), defender.types)
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


# ── Battle scene image rendering ────────────────────────────────────────────

CANVAS_W, CANVAS_H = 720, 380
SKY_COLOR = (135, 206, 235, 255)
GROUND_COLOR = (150, 210, 90, 255)
SPRITE_SCALE = 3

_FONT_CACHE: dict = {}


def _font(size: int, bold: bool = True):
    if not PIL_OK:
        return None
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
                 ("Arial Bold.ttf" if bold else "Arial.ttf")):
        try:
            f = ImageFont.truetype(name, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _hp_color(frac: float):
    if frac > 0.5:
        return (60, 200, 70, 255)
    if frac > 0.2:
        return (240, 190, 40, 255)
    return (220, 60, 60, 255)


async def _fetch_sprite(session: aiohttp.ClientSession, url: str):
    if not url or not PIL_OK:
        return None
    try:
        async with session.get(url, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.read()
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None


def _draw_hp_bar_above(draw: "ImageDraw.ImageDraw", center_x: float, sprite_top_y: float,
                        name: str, hp: int, max_hp: int, width: int = 190):
    """Draws a compact name+HP-bar plate directly above a sprite's position."""
    bar_h = 10
    plate_h = 40
    x = int(center_x - width / 2)
    x = max(6, min(x, CANVAS_W - width - 6))
    y = int(max(4, sprite_top_y - plate_h - 6))

    draw.rounded_rectangle([x, y, x + width, y + plate_h], radius=9,
                            fill=(255, 255, 255, 235), outline=(40, 40, 40, 255), width=2)
    draw.text((x + 10, y + 4), f"{name.title()}  Lv.{LEVEL}",
               font=_font(14), fill=(20, 20, 20, 255))

    bar_x, bar_y, bar_w = x + 10, y + 22, width - 20
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                            radius=5, fill=(90, 90, 90, 255))
    frac = max(hp, 0) / max_hp if max_hp else 0
    fill_w = int(bar_w * frac)
    if fill_w > 0:
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                                radius=5, fill=_hp_color(frac))


async def render_battle_scene(session: aiohttp.ClientSession,
                               opponent_pokemon: "BattlePokemon",
                               player_pokemon: "BattlePokemon") -> Optional[discord.File]:
    """Classic side-on battle scene: opponent upper-right facing player,
    player's pokemon lower-left (mirrored) facing opponent, with an HP bar
    rendered directly above each sprite (name, Lv.100, colour-shifting bar)."""
    if not PIL_OK:
        return None

    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), SKY_COLOR)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, CANVAS_H - 120, CANVAS_W, CANVAS_H], fill=GROUND_COLOR)
    draw.ellipse([CANVAS_W * 0.55 - 150, CANVAS_H - 150, CANVAS_W * 0.55 + 150, CANVAS_H - 90],
                 fill=(130, 190, 75, 255))
    draw.ellipse([CANVAS_W * 0.14 - 120, CANVAS_H - 95, CANVAS_W * 0.14 + 120, CANVAS_H - 45],
                 fill=(130, 190, 75, 255))

    opp_sprite = await _fetch_sprite(session, opponent_pokemon.sprite)
    player_sprite = await _fetch_sprite(session, player_pokemon.sprite)

    opp_pos = (int(CANVAS_W * 0.60), 68)
    player_pos = (int(CANVAS_W * 0.06), CANVAS_H - 232)

    opp_w = 96 * SPRITE_SCALE
    if opp_sprite:
        opp_sprite = opp_sprite.resize(
            (opp_sprite.width * SPRITE_SCALE, opp_sprite.height * SPRITE_SCALE), Image.NEAREST)
        canvas.alpha_composite(opp_sprite, opp_pos)
        opp_w = opp_sprite.width

    player_w = 96 * SPRITE_SCALE
    if player_sprite:
        player_sprite = ImageOps.mirror(player_sprite)
        player_sprite = player_sprite.resize(
            (player_sprite.width * SPRITE_SCALE, player_sprite.height * SPRITE_SCALE), Image.NEAREST)
        canvas.alpha_composite(player_sprite, player_pos)
        player_w = player_sprite.width

    _draw_hp_bar_above(draw, opp_pos[0] + opp_w / 2, opp_pos[1],
                        opponent_pokemon.name, opponent_pokemon.hp, opponent_pokemon.max_hp)
    _draw_hp_bar_above(draw, player_pos[0] + player_w / 2, player_pos[1],
                        player_pokemon.name, player_pokemon.hp, player_pokemon.max_hp)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="battle.png")


# ── Trainer / pending challenge ─────────────────────────────────────────────

@dataclass
class Trainer:
    user: discord.abc.User
    team: list = field(default_factory=list)
    active_idx: int = 0
    is_bot: bool = False  # True for the bot's own side in a `!battle @bot` fight

    @property
    def active(self) -> BattlePokemon:
        return self.team[self.active_idx]

    @property
    def alive_team(self) -> list:
        return [p for p in self.team if not p.fainted]


def estimate_damage(attacker: BattlePokemon, defender: BattlePokemon, move: dict) -> float:
    """Deterministic damage estimate for AI move comparison — same formula
    as calc_damage() but without the random crit roll or the 0.85-1.0
    damage-roll variance, so moves can be ranked consistently."""
    power = move.get("power") or 0
    if power <= 0:
        return 0.0
    if move.get("damage_class") == "physical":
        a, d = attacker.effective_stat("atk"), defender.effective_stat("dfn")
    else:
        a, d = attacker.effective_stat("spa"), defender.effective_stat("spd")
    stab = 1.5 if move.get("type") in attacker.types else 1.0
    eff = type_multiplier(move.get("type", "normal"), defender.types)
    if eff == 0:
        return 0.0
    base = (((2 * LEVEL / 5 + 2) * power * a / max(d, 1)) / 50 + 2)
    return base * stab * eff


def bot_choose_action(trainer: Trainer, opponent: Trainer) -> tuple:
    """Battle AI for the bot's own trainer: calculates the expected damage
    of every move in its active Pokemon's (already-curated) move pool
    against the opponent's current active Pokemon — factoring in STAB,
    type effectiveness, and the attacker/defender's effective stats — and
    attacks with whichever move deals the most damage. No switching logic
    beyond forced switches on faint — see Battle.get_forced_switch."""
    moves = trainer.active.moves
    defender = opponent.active
    scores = [estimate_damage(trainer.active, defender, m) for m in moves]
    best_idx = max(range(len(moves)), key=lambda i: scores[i])
    return ("move", best_idx)


@dataclass
class PendingChallenge:
    challenger: discord.Member
    opponent: discord.Member
    fmt: str
    count: int
    accepted: bool = False
    teams: dict = field(default_factory=dict)
    bst_filter: tuple = (None, None)


# ── UI: challenge accept/decline ────────────────────────────────────────────

class ChallengeView(discord.ui.View):
    def __init__(self, cog: "BattleCog", challenger: discord.Member,
                 opponent: discord.Member, fmt: str, count: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.challenger = challenger
        self.opponent = opponent
        self.fmt = fmt
        self.count = count

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message(
                "This challenge isn't addressed to you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.cog.pending.pop(getattr(self, "_channel_id", None), None)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="⚔️")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ {self.opponent.mention} accepted the challenge!", view=None)
        await self.cog.start_challenge(interaction.channel, self.challenger,
                                        self.opponent, self.fmt, self.count)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="🚫")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        self.cog.pending.pop(interaction.channel.id, None)
        await interaction.response.edit_message(
            content=f"❌ {self.opponent.mention} declined the challenge.", view=None)


# ── UI: forced switch after a faint (visible message, restricted to owner) ──

class ForcedSwitchButton(discord.ui.Button):
    def __init__(self, label: str, idx: int):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        view: "ForcedSwitchView" = self.view
        for item in view.children:
            item.disabled = True
        await interaction.response.edit_message(view=view)
        if not view.future.done():
            view.future.set_result(self.idx)
        view.stop()


class ForcedSwitchView(discord.ui.View):
    """Shown publicly (mentioning the trainer) when their active Pokemon
    faints mid-turn and they must send out a replacement. Visible to
    everyone, but only the owning trainer can press a button."""

    def __init__(self, trainer: Trainer, future: asyncio.Future):
        super().__init__(timeout=60)
        self.trainer = trainer
        self.future = future
        self._alive_indices = []
        for i, p in enumerate(trainer.team):
            if not p.fainted:
                self._alive_indices.append(i)
                self.add_item(ForcedSwitchButton(p.name.title(), i))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.trainer.user.id:
            await interaction.response.send_message("Not your Pokemon to switch.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if not self.future.done() and self._alive_indices:
            self.future.set_result(self._alive_indices[0])


# ── UI: private (ephemeral) switch menu, opened from the turn panel ────────

class SwitchSelectView(discord.ui.View):
    """Sent as an ephemeral response when a trainer presses the panel's
    Switch button — only that trainer ever sees this menu, so their bench
    isn't revealed to the opponent while they decide."""

    def __init__(self, panel: "BattlePanel", trainer: Trainer, bench: list):
        super().__init__(timeout=TURN_TIMEOUT)
        self.panel = panel
        self.trainer = trainer
        select = discord.ui.Select(
            placeholder="Choose your next Pokémon",
            options=[
                discord.SelectOption(
                    label=f"{p.name.title()} (Lv.{LEVEL})",
                    description=f"{p.hp}/{p.max_hp} HP",
                    value=str(i),
                )
                for i, p in bench
            ],
        )
        select.callback = self._callback
        self.add_item(select)
        self._select = select

    async def _callback(self, interaction: discord.Interaction):
        idx = int(self._select.values[0])
        for item in self.children:
            item.disabled = True
        mon = self.trainer.team[idx].name.title()
        await interaction.response.edit_message(
            content=f"✅ You'll send out **{mon}** this turn (this uses your whole turn).",
            view=self,
        )
        await self.panel.set_action(interaction, self.trainer, ("switch", idx),
                                     via_separate_message=True)
        self.stop()


# ── UI: the single combined turn panel ──────────────────────────────────────

class MoveSelect(discord.ui.Select):
    def __init__(self, trainer: Trainer, panel: "BattlePanel", row: int):
        self.trainer = trainer
        self.panel = panel
        options = []
        for i, mv in enumerate(trainer.active.moves):
            tag = "⚡Priority • " if mv.get("priority", 0) > 0 else ""
            desc = f"{tag}{mv.get('type', 'normal').title()} • {mv.get('power') or '—'} power"
            options.append(discord.SelectOption(
                label=mv["name"].replace("-", " ").title()[:100],
                description=desc[:100],
                value=str(i),
            ))
        super().__init__(
            placeholder=f"{trainer.user.display_name}: choose {trainer.active.name.title()}'s move",
            min_values=1, max_values=1, options=options, row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.trainer.user.id:
            await interaction.response.send_message(
                "🚫 That's not your Pokémon to command.", ephemeral=True)
            return
        if self.trainer.user.id in self.panel.actions:
            await interaction.response.send_message(
                "You've already locked in your action this turn.", ephemeral=True)
            return
        idx = int(self.values[0])
        await self.panel.set_action(interaction, self.trainer, ("move", idx))


class SwitchButton(discord.ui.Button):
    def __init__(self, panel: "BattlePanel"):
        super().__init__(label="Switch", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        trainer = self.panel.trainer_for(interaction.user.id)
        if trainer is None:
            await interaction.response.send_message("You're not part of this battle.", ephemeral=True)
            return
        if trainer.user.id in self.panel.actions:
            await interaction.response.send_message(
                "You've already locked in your action this turn.", ephemeral=True)
            return
        bench = [(i, p) for i, p in enumerate(trainer.team)
                 if not p.fainted and i != trainer.active_idx]
        if not bench:
            await interaction.response.send_message(
                "You have no other healthy Pokémon to switch to!", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose a Pokémon to switch in — only you can see this menu:",
            view=SwitchSelectView(self.panel, trainer, bench),
            ephemeral=True,
        )


class PassButton(discord.ui.Button):
    def __init__(self, panel: "BattlePanel"):
        super().__init__(label="Pass Turn", emoji="⏭️", style=discord.ButtonStyle.secondary, row=2)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        trainer = self.panel.trainer_for(interaction.user.id)
        if trainer is None:
            await interaction.response.send_message("You're not part of this battle.", ephemeral=True)
            return
        if trainer.user.id in self.panel.actions:
            await interaction.response.send_message(
                "You've already locked in your action this turn.", ephemeral=True)
            return
        await self.panel.set_action(interaction, trainer, ("pass", None))


class BattlePanel(discord.ui.View):
    """The one combined view posted each turn: two move dropdowns (one per
    trainer), a shared Switch button, and a shared Pass Turn button.

    Each trainer locks in exactly one action per turn, stored in
    `self.actions[user_id] = (kind, value)` where kind is "move", "switch",
    or "pass". Switch and move are mutually exclusive for a given trainer
    in a given turn — a switched-in Pokemon is never also made to attack,
    which is what previously caused the "random move on switch" bug.
    """

    def __init__(self, t1: Trainer, t2: Trainer):
        super().__init__(timeout=TURN_TIMEOUT)
        self.t1 = t1
        self.t2 = t2
        self.actions: dict = {}
        self.event = asyncio.Event()
        self.message: Optional[discord.Message] = None

        # A bot-controlled trainer gets no dropdown and no wait — its move
        # is decided immediately via bot_choose_action() instead of a
        # component interaction, since nobody is going to click for it.
        self.move_select_t1 = None
        self.move_select_t2 = None
        row = 0
        if t1.is_bot:
            self.actions[t1.user.id] = bot_choose_action(t1, t2)
        else:
            self.move_select_t1 = MoveSelect(t1, self, row=row)
            self.add_item(self.move_select_t1)
            row += 1
        if t2.is_bot:
            self.actions[t2.user.id] = bot_choose_action(t2, t1)
        else:
            self.move_select_t2 = MoveSelect(t2, self, row=row)
            self.add_item(self.move_select_t2)
            row += 1

        self.add_item(SwitchButton(self))
        self.add_item(PassButton(self))

        if len(self.actions) == 2 and not self.event.is_set():
            self.event.set()

    def trainer_for(self, user_id: int) -> Optional[Trainer]:
        if user_id == self.t1.user.id:
            return self.t1
        if user_id == self.t2.user.id:
            return self.t2
        return None

    def _select_for(self, trainer: Trainer) -> MoveSelect:
        return self.move_select_t1 if trainer is self.t1 else self.move_select_t2

    async def set_action(self, interaction: discord.Interaction, trainer: Trainer,
                          action: tuple, via_separate_message: bool = False):
        self.actions[trainer.user.id] = action
        verb = {"move": "chose a move", "switch": "will switch", "pass": "passed"}[action[0]]
        sel = self._select_for(trainer)
        sel.disabled = True
        sel.placeholder = f"{trainer.user.display_name} {verb} ✅"

        ready = len(self.actions) == 2
        if ready:
            for item in self.children:
                item.disabled = True

        if via_separate_message:
            # This action came from the private ephemeral switch menu, not
            # from a component on the panel message itself — edit the panel
            # message directly instead of trying to ack this interaction.
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass
        else:
            await interaction.response.edit_message(view=self)

        if ready and not self.event.is_set():
            self.event.set()

    async def on_timeout(self):
        # Fill in any missing action with that trainer's strongest move
        # (moves are pre-sorted by power) instead of a random one.
        for trainer in (self.t1, self.t2):
            if trainer.user.id not in self.actions:
                self.actions[trainer.user.id] = ("move", 0)
                sel = self._select_for(trainer)
                sel.disabled = True
                sel.placeholder = f"{trainer.user.display_name} ran out of time — auto-used strongest move"
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        if not self.event.is_set():
            self.event.set()


# ── Battle runner ────────────────────────────────────────────────────────────

class Battle:
    def __init__(self, cog: "BattleCog", channel: discord.TextChannel,
                 t1: Trainer, t2: Trainer):
        self.cog = cog
        self.channel = channel
        self.t1 = t1
        self.t2 = t2

    async def build_embed(self, turn: int, last_summary: Optional[str],
                           final: bool = False, winner: Optional[Trainer] = None):
        file = await render_battle_scene(self.cog.session, self.t2.active, self.t1.active)

        embed = discord.Embed(
            title=("🏆 Battle Complete" if final else f"⚔️ Turn {turn}"),
            colour=(0xF1C40F if final else 0x3498DB),
        )
        if winner is not None:
            embed.description = f"**{winner.user.display_name} wins the battle!**"
        if last_summary:
            embed.add_field(name="📋 Last Turn's Results", value=last_summary[:1024], inline=False)

        for t in (self.t1, self.t2):
            p = t.active
            embed.add_field(
                name=t.user.display_name,
                value=(f"{p.name.title()} (Lv.{LEVEL})\n"
                       f"❤️ {p.hp}/{p.max_hp} HP\n"
                       f"Team remaining: {len(t.alive_team)}/{len(t.team)}"),
                inline=True,
            )

        if file is not None:
            embed.set_image(url="attachment://battle.png")
        else:
            embed.add_field(name="⚠️ Note", value="Pillow isn't installed — image disabled.",
                             inline=False)
        return embed, file

    def build_results_embed(self, last_summary: str) -> discord.Embed:
        """Plain text-only embed recapping the previous turn's damage,
        switches, etc. — sent on its own between the scene reveal and the
        next action panel."""
        return discord.Embed(
            title="📋 Last Turn's Results",
            description=last_summary[:4096],
            colour=0x95A5A6,
        )

    async def _send_embed(self, embed: discord.Embed, file: Optional[discord.File], **kwargs):
        if file is not None:
            return await self.channel.send(embed=embed, file=file, **kwargs)
        return await self.channel.send(embed=embed, **kwargs)

    def _execute_move(self, attacker: Trainer, defender: Trainer, move: dict) -> list:
        """Resolves one move: accuracy check, damage, secondary stat
        effects, recoil/drain, and self-KO moves. Returns a list of
        flavor-text lines (usually 1-3) describing everything that
        happened."""
        move_name = move["name"].replace("-", " ").title()

        acc = move.get("accuracy")
        if acc is not None and random.uniform(0, 100) > acc:
            return [f"❌ {attacker.active.name.title()}'s {move_name} missed!"]

        dmg, eff, crit = calc_damage(attacker.active, defender.active, move)
        defender.active.hp = max(0, defender.active.hp - dmg)

        text = f"➡️ {attacker.active.name.title()} used **{move_name}**! (**{dmg}** dmg)"
        if crit:
            text += " 💫 Critical hit!"
        if eff > 1:
            text += " It's super effective!"
        elif 0 < eff < 1:
            text += " It's not very effective..."
        elif eff == 0:
            text += " It had no effect!"
        lines = [text]

        # Self-KO moves: Explosion / Self-Destruct faint the user outright.
        if move["name"] in SELF_KO_MOVES:
            attacker.active.hp = 0
            lines.append(f"💥 {attacker.active.name.title()} was consumed by the blast!")
        else:
            recoil_msg = _apply_drain_recoil(attacker.active, dmg, move)
            if recoil_msg:
                lines.append(recoil_msg)

        lines.extend(_apply_secondary_effects(attacker.active, defender.active, move))
        return lines

    async def get_forced_switch(self, trainer: Trainer) -> int:
        if trainer.is_bot:
            # No UI to show — just send out its next healthy Pokemon.
            for i, p in enumerate(trainer.team):
                if not p.fainted:
                    return i
        future = asyncio.get_event_loop().create_future()
        view = ForcedSwitchView(trainer, future)
        await self.channel.send(
            f"{trainer.user.mention}, **{trainer.active.name.title()}** fainted! "
            f"Choose your next Pokemon:",
            view=view,
        )
        return await future

    async def run(self):
        await self.channel.send(
            f"⚔️ **Battle start!** {self.t1.user.mention} vs {self.t2.user.mention} "
            f"— all Pokémon are Level {LEVEL}."
        )

        turn = 1
        last_summary: Optional[str] = None
        pending_switches: list = []  # trainers whose active fainted last turn

        while self.t1.alive_team and self.t2.alive_team:
            # 1) Recap last turn's results as a plain text-only embed
            #    (nothing to recap yet on turn 1).
            if last_summary:
                await self.channel.send(embed=self.build_results_embed(last_summary))
                await asyncio.sleep(3)

            # 1.5) Now that the damage recap has been shown (so it's clear
            #    *why*), prompt any trainer whose active Pokemon fainted
            #    last turn to send out a replacement.
            for trainer in pending_switches:
                new_idx = await self.get_forced_switch(trainer)
                trainer.active_idx = new_idx
                await self.channel.send(
                    f"{trainer.user.display_name} sent out **{trainer.active.name.title()}**!"
                )
            pending_switches = []

            # 2) The actual actionable panel: image + trainer info + move
            #    dropdowns, same as before.
            panel = BattlePanel(self.t1, self.t2)
            embed, file = await self.build_embed(turn, None)
            msg = await self._send_embed(
                embed, file,
                content=f"{self.t1.user.mention} {self.t2.user.mention} — choose your action.",
                view=panel,
            )
            panel.message = msg

            await panel.event.wait()

            lines: list = []

            # 1) Switches resolve first and consume the whole turn for that
            #    trainer — a switched-in Pokemon never also attacks.
            for trainer in (self.t1, self.t2):
                action = panel.actions.get(trainer.user.id)
                if action and action[0] == "switch":
                    old_name = trainer.active.name.title()
                    trainer.active_idx = action[1]
                    lines.append(
                        f"🔄 {trainer.user.display_name} withdrew {old_name} and sent out "
                        f"**{trainer.active.name.title()}**!"
                    )
                elif action and action[0] == "pass":
                    lines.append(f"⏭️ {trainer.user.display_name} passed the turn.")

            # 2) Moves resolve in priority/speed order. Only trainers whose
            #    locked-in action was "move" attack this turn.
            movers = []
            for trainer, opponent in ((self.t1, self.t2), (self.t2, self.t1)):
                action = panel.actions.get(trainer.user.id)
                if action and action[0] == "move":
                    move = trainer.active.moves[action[1]]
                    # Capture the actual Pokemon object whose move this is,
                    # not just the trainer — trainer.active_idx can change
                    # mid-loop below (a forced switch after an earlier
                    # mover's KO), and we need to tell a stale queued move
                    # apart from a freshly-sent-in replacement.
                    movers.append((trainer, opponent, move, trainer.active))
            # Priority moves always go first; ties within the same priority
            # bracket go to the faster Pokemon (using effective, stage-
            # boosted Speed). Because _calc_stat truncates to an int, two
            # Pokemon with different base Speed can land on the exact same
            # computed spe — so before ever touching randomness we break
            # that with the precise raw base Speed (higher precision,
            # never truncated) so the objectively-faster Pokemon reliably
            # goes first every turn instead of the order flipping randomly
            # turn to turn. Only a genuine full tie (same base Speed too)
            # falls to random.random(), broken fresh each turn rather than
            # always favoring the same trainer.
            movers.sort(
                key=lambda o: (
                    o[2].get("priority", 0),
                    o[0].active.effective_stat("spe"),
                    o[0].active.base_speed,
                    random.random(),
                ),
                reverse=True,
            )

            for attacker, defender, move, acting_pokemon in movers:
                if attacker.active is not acting_pokemon:
                    # attacker's original Pokemon already fainted and was
                    # forced-switched out by an earlier, faster mover this
                    # same turn — the replacement only came in to fill the
                    # empty slot, it doesn't also get to attack this turn.
                    continue
                if attacker.active.fainted or defender.active.fainted:
                    continue
                move_name = move["name"]
                lines.extend(self._execute_move(attacker, defender, move))

                if attacker.active.fainted and move_name not in SELF_KO_MOVES:
                    # Fainted from its own recoil.
                    lines.append(f"💥 {attacker.active.name.title()} fainted from recoil!")
                if attacker.active.fainted and attacker.alive_team:
                    # Don't prompt for a replacement yet — that happens at
                    # the top of the next iteration, after the damage
                    # recap embed has been shown.
                    pending_switches.append(attacker)

                if defender.active.fainted:
                    lines.append(f"💥 {defender.active.name.title()} fainted!")
                    if defender.alive_team:
                        pending_switches.append(defender)

            last_summary = "\n".join(lines) if lines else "No actions were taken."
            turn += 1

        # Battle's over — if the winner's own active happened to faint on
        # this final turn too (a mutual KO), silently slot in their next
        # healthy Pokemon so the final embed doesn't show a fainted mon.
        for trainer in pending_switches:
            if trainer.alive_team:
                for i, p in enumerate(trainer.team):
                    if not p.fainted:
                        trainer.active_idx = i
                        break

        winner = self.t1 if self.t1.alive_team else self.t2
        loser = self.t2 if winner is self.t1 else self.t1

        final_embed, final_file = await self.build_embed(turn, last_summary,
                                                           final=True, winner=winner)
        await self._send_embed(final_embed, final_file)
        await self.channel.send(
            f"🏆 **{winner.user.display_name} wins the battle!** GG {loser.user.mention}."
        )
        self.cog.active_battles.pop(self.channel.id, None)


# ── Cog ───────────────────────────────────────────────────────────────────

class BattleCog(commands.Cog, name="Battle"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.pending: dict = {}          # channel_id -> PendingChallenge
        self.active_battles: dict = {}   # channel_id -> Battle

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    @commands.group(name="battle", invoke_without_command=True)
    async def battle(self, ctx: commands.Context,
                      opponent: Optional[discord.Member] = None,
                      fmt: str = "random", count: int = 3,
                      *, bst_filter: Optional[str] = None):
        if opponent is None:
            await ctx.send(
                "Usage: `!battle @user [random|custom] [count 1-6] [>min<max]`\n"
                "The `>min<max` part is optional and filters `random` teams by "
                "base stat total, e.g. `!battle @user random 3 >590<700`. You "
                "can also battle me directly: `!battle @<my name> random 3 >550`."
            )
            return
        if opponent.id == ctx.author.id:
            await ctx.send("Pick a real opponent (not yourself).")
            return
        battling_bot = opponent.id == self.bot.user.id
        if opponent.bot and not battling_bot:
            await ctx.send("Pick a real opponent (not another bot).")
            return
        if ctx.channel.id in self.pending or ctx.channel.id in self.active_battles:
            await ctx.send(
                "There's already a pending challenge or active battle in this "
                "channel. Finish it or run `!battle cancel` first."
            )
            return

        fmt = fmt.lower()
        if fmt not in ("random", "custom"):
            await ctx.send("Format must be `random` or `custom`.")
            return
        if battling_bot and fmt != "random":
            await ctx.send("You can only battle me in `random` format.")
            return
        count = max(1, min(6, count))
        min_total, max_total = parse_bst_filter(bst_filter)
        if bst_filter and fmt == "custom" and (min_total is not None or max_total is not None):
            await ctx.send("⚠️ The BST filter only applies to `random` teams — ignoring it for this custom battle.")

        if battling_bot:
            filt_note = format_bst_filter(min_total, max_total)
            await ctx.send(
                f"🎲 Rolling random teams — {ctx.author.mention} vs me!{filt_note}"
            )
            t1 = Trainer(ctx.author)
            t2 = Trainer(self.bot.user, is_bot=True)
            for _ in range(count):
                t1.team.append(await build_random_pokemon(self.session, min_total, max_total))
                t2.team.append(await build_random_pokemon(self.session, min_total, max_total))
            battle = Battle(self, ctx.channel, t1, t2)
            self.active_battles[ctx.channel.id] = battle
            await battle.run()
            return

        self.pending[ctx.channel.id] = PendingChallenge(
            ctx.author, opponent, fmt, count, bst_filter=(min_total, max_total)
        )

        view = ChallengeView(self, ctx.author, opponent, fmt, count)
        view._channel_id = ctx.channel.id
        filt_note = format_bst_filter(min_total, max_total) if fmt == "random" else ""
        await ctx.send(
            f"⚔️ {ctx.author.mention} has challenged {opponent.mention} to a "
            f"**{fmt}** battle ({count} pokemon each, Level {LEVEL}){filt_note}! "
            f"{opponent.mention}, do you accept?",
            view=view,
        )

    async def start_challenge(self, channel, challenger, opponent, fmt, count):
        pending = self.pending.get(channel.id)
        if not pending:
            return

        if fmt == "random":
            min_total, max_total = pending.bst_filter
            filt_note = format_bst_filter(min_total, max_total)
            await channel.send(f"🎲 Rolling random teams...{filt_note}")
            t1, t2 = Trainer(challenger), Trainer(opponent)
            for _ in range(count):
                t1.team.append(await build_random_pokemon(self.session, min_total, max_total))
                t2.team.append(await build_random_pokemon(self.session, min_total, max_total))
            self.pending.pop(channel.id, None)
            battle = Battle(self, channel, t1, t2)
            self.active_battles[channel.id] = battle
            await battle.run()
        else:
            pending.accepted = True
            await channel.send(
                f"📋 Custom battle! Both trainers build your team with:\n"
                f"`!battle add pikachu, charizard, ...` (up to {count} each)\n"
                f"{challenger.mention} and {opponent.mention}, go ahead."
            )

    @battle.command(name="add")
    async def battle_add(self, ctx: commands.Context, *, names: str):
        pending = self.pending.get(ctx.channel.id)
        if not pending or not pending.accepted or pending.fmt != "custom":
            await ctx.send("There's no pending custom battle here to add Pokemon to.")
            return
        if ctx.author.id not in (pending.challenger.id, pending.opponent.id):
            await ctx.send("You're not part of this battle.")
            return

        team = pending.teams.setdefault(ctx.author.id, [])
        if len(team) >= pending.count:
            await ctx.send(f"You already have your full team of {pending.count}.")
            return

        requested = [n.strip() for n in names.split(",") if n.strip()]
        added, failed = [], []
        async with ctx.typing():
            for nm in requested:
                if len(team) >= pending.count:
                    failed.append(f"{nm} (team already full)")
                    continue
                data = await get_pokemon_data(self.session, nm)
                if not data:
                    failed.append(f"{nm} (not found)")
                    continue
                moves = await pick_moves(self.session, data)
                team.append(BattlePokemon(data, moves))
                added.append(data["name"].title())

        msg = ""
        if added:
            msg += f"✅ Added: {', '.join(added)} ({len(team)}/{pending.count})\n"
        if failed:
            msg += f"⚠️ Skipped: {', '.join(failed)}"
        await ctx.send(msg or "Nothing added.")

        challenger_team = pending.teams.get(pending.challenger.id, [])
        opponent_team = pending.teams.get(pending.opponent.id, [])
        if len(challenger_team) >= pending.count and len(opponent_team) >= pending.count:
            self.pending.pop(ctx.channel.id, None)
            t1 = Trainer(pending.challenger, team=challenger_team)
            t2 = Trainer(pending.opponent, team=opponent_team)
            battle = Battle(self, ctx.channel, t1, t2)
            self.active_battles[ctx.channel.id] = battle
            await ctx.send("Both teams are ready — battle starting!")
            await battle.run()

    @battle.command(name="cancel")
    async def battle_cancel(self, ctx: commands.Context):
        if ctx.channel.id in self.pending:
            del self.pending[ctx.channel.id]
            await ctx.send("Challenge cancelled.")
        elif ctx.channel.id in self.active_battles:
            del self.active_battles[ctx.channel.id]
            await ctx.send("Battle force-ended.")
        else:
            await ctx.send("Nothing to cancel here.")


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))
