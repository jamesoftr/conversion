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
!battle ai [count] [>min<max]
    Battle the bot itself instead of another user — no Accept/Decline step,
    the battle starts immediately. `ai` (or `bot`) works as a shorthand for
    mentioning the bot by name. The bot plays its own team with an AI
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
    in the current channel with no winner/loser recorded.

!battle forfeit
    Forfeits the active battle in this channel — unlike `!battle cancel`,
    the other trainer is declared the winner and stats/Elo are recorded
    normally. A trainer who misses 2 turns in a row (no action before the
    90s timer) is also auto-forfeited, so a stalled/AFK trainer can't
    stall a battle forever.

!pf [@user]
    Shows a trainer's all-time battle record (defaults to yourself),
    split into "Vs Humans" (PvP) and "Vs AI" (`!battle @<bot's name>`)
    totals/wins/losses.

!pf ai
    Shows the BOT's own global record — total battles, wins, and losses
    across every `!battle @<bot>` fight anyone in the server has played
    against it. (The flip side of everyone's individual "Vs AI" stats.)

!elo [@user]
    Shows a trainer's Elo battle rating (starts at 1000). A win nets more
    rating than a loss costs, and losses cost more the higher your own
    rating climbs — but a loss is never worth less than a small floor.
    Rated for every battle, including against the bot.

!elo lb
    Shows the server's Elo leaderboard (top 10), bot included.

Rematch
    After a battle ends, a "🔁 Rematch" button is posted that recreates
    the exact same matchup/format/team size/BST filter. Against another
    human both trainers must click it; against the bot, just the human.

Battle mechanics
-----------------
On top of accuracy checks, priority/speed turn order, STAB/type
effectiveness, stat stage changes, and drain/recoil, the engine also
models:
  • Status conditions — burn, paralysis, poison, sleep, freeze. A
    Pokemon's moveset guarantees one reliable status-inducing move
    (Thunder Wave/Toxic/Will-O-Wisp/Spore-tier) when it learns one, on
    top of the % chance secondary effects some damaging moves carry
    (e.g. Thunderbolt's 10% paralyze).
  • PP — each move tracks its own remaining PP; a Pokemon out of PP on
    every move is forced to Struggle (25% max-HP recoil).
  • A handful of common abilities: Levitate/Water Absorb/Volt
    Absorb/Flash Fire (type immunities, the absorb ones healing instead),
    Intimidate (Attack drop on switch-in), Guts (Atk boost while
    statused, turns burn's penalty into a bonus), and Sturdy (survives an
    OHKO from full HP with 1 HP). Weather and the rest of the ability
    roster are still out of scope.
  • Held items — each Pokemon has a chance to be holding one. Kept
    deliberately mild/sustain-only (no damage or crit boosters, no OHKO
    survival items): Leftovers (heals 1/16 max HP every end of turn),
    Oran Berry / Sitrus Berry (one-shot heal — 1/8 or 1/4 max HP — the
    first time HP drops to half or below), and Shell Bell (heals the
    holder 1/8 of any damage it deals).
  • The `!battle @<bot>` AI weighs moves by accuracy-discounted expected
    damage (not just raw power) and can voluntarily switch out of a bad
    matchup, not just when forced by a faint.

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
`pick_moves()` fetches every move in a Pokemon's learnable move pool and
keeps the top 4, sorted primarily by base power — so each Pokemon
generally has its hardest-hitting moves available, not a random sample.
Ahead of pure power, a few slots are guaranteed if available: the best
priority move, the best STAB move per type, and one reliable
status-inducing move (see "Battle mechanics" below).
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


# ── Elo rating ────────────────────────────────────────────────────────────
# Stored in its own collection (battle_elo), keyed by "<guild_id>:<user_id>",
# independent of the existing win/loss stats in db.py.
DEFAULT_ELO = 1000
ELO_K_WIN = 24        # base gain on a win
ELO_K_LOSS = 16       # base cost of a loss — smaller than the win gain, so
                       # winning nets more than losing costs on average
ELO_MIN_LOSS_PENALTY = 6   # a loss always costs at least this much
ELO_MAX_DELTA = 40         # ...but never swings more than this in one battle
ELO_FLOOR = 100


async def _get_elo(guild_id: int, user_id: int) -> int:
    doc = await _col().battle_elo.find_one({"_id": f"{guild_id}:{user_id}"})
    return doc["elo"] if doc else DEFAULT_ELO


async def _set_elo(guild_id: int, user_id: int, elo: int):
    await _col().battle_elo.update_one(
        {"_id": f"{guild_id}:{user_id}"},
        {"$set": {"guild_id": guild_id, "user_id": user_id, "elo": elo}},
        upsert=True,
    )


def _elo_delta(my_elo: int, opp_elo: int, won: bool) -> int:
    """Rating change for one side of a result.

    - Standard elo expected-score scaling: beating a higher-rated opponent
      nets more, losing to a lower-rated one costs more.
    - A win nets more than a loss costs at the same rating (K_WIN > K_LOSS)
      — winning is rewarded more than losing is punished.
    - Losses get progressively harsher the higher your OWN rating climbs
      (staying at the top has to be earned), but a loss is never trivial —
      it always costs at least ELO_MIN_LOSS_PENALTY, even for a huge
      underdog.
    """
    expected = 1 / (1 + 10 ** ((opp_elo - my_elo) / 400))
    if won:
        raw = ELO_K_WIN * (1 - expected)
        delta = max(5, round(raw))
    else:
        raw = ELO_K_LOSS * expected
        raw += max(0, my_elo - DEFAULT_ELO) / 100  # higher rating -> losses sting more
        delta = max(ELO_MIN_LOSS_PENALTY, round(raw))
    return min(ELO_MAX_DELTA, int(delta))


async def apply_elo_result(guild_id: int, winner_id: int, loser_id: int):
    """Updates and returns (new_winner_elo, winner_delta, new_loser_elo, loser_delta)."""
    w_elo = await _get_elo(guild_id, winner_id)
    l_elo = await _get_elo(guild_id, loser_id)
    w_delta = _elo_delta(w_elo, l_elo, won=True)
    l_delta = _elo_delta(l_elo, w_elo, won=False)
    new_w = w_elo + w_delta
    new_l = max(ELO_FLOOR, l_elo - l_delta)
    await _set_elo(guild_id, winner_id, new_w)
    await _set_elo(guild_id, loser_id, new_l)
    return new_w, w_delta, new_l, -(l_elo - new_l)


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
    # Non-hidden abilities only, kept as a list — BattlePokemon randomly
    # picks one of these on construction so repeat battles with the same
    # cached species still see natural variety (e.g. a Gyarados that's
    # sometimes Intimidate, sometimes Moxie).
    abilities = [a["ability"]["name"] for a in data.get("abilities", []) if not a.get("is_hidden")]

    doc = {
        "_id": key,
        "name": data["name"],
        "dex_id": data["id"],
        "types": types,
        "stats": stats,
        "move_pool": move_pool,
        "sprite": sprite,
        "abilities": abilities,
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
        # Status-ailment data (paralysis/sleep/freeze/burn/poison etc.) —
        # ailment_chance of 0 on a status-class move conventionally means
        # "always applies" (gated only by the move's own accuracy); a
        # nonzero chance is a secondary effect on a damage-dealing move
        # (e.g. Thunderbolt's 10% paralyze).
        "ailment": ((data.get("meta") or {}).get("ailment") or {}).get("name"),
        "ailment_chance": (data.get("meta") or {}).get("ailment_chance", 0),
    }
    await _col().move_cache.update_one({"_id": key}, {"$set": doc}, upsert=True)
    return doc


PRIORITY_MOVE_MIN_POWER = 40  # e.g. Quick Attack/Aqua Jet/Mach Punch-tier or better
STAB_MOVE_MIN_POWER = 70  # guarantee a slot for own-type moves above this power
STATUS_INDUCING_AILMENTS = {"paralysis", "sleep", "freeze", "burn", "poison"}


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

    One more guarantee: if the Pokemon can learn a reliable status move
    (Thunder Wave, Toxic, Will-O-Wisp, Spore, ...) that inflicts one of the
    five modeled ailments (paralysis/sleep/freeze/burn/poison), the
    highest-accuracy one of those gets a slot too, so status isn't a
    mechanic that only ever shows up as a rare secondary effect.
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
    status_candidates = [
        mv for mv in results
        if isinstance(mv, dict) and mv.get("damage_class") == "status"
        and mv.get("ailment") in STATUS_INDUCING_AILMENTS
        and not mv.get("ailment_chance")  # only the move's own guaranteed effect, not a % secondary
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

    # Status guarantee: one reliable status-inducing move, if there's
    # still room and the Pokemon actually learns one.
    if len(chosen) < count and status_candidates:
        remaining_status = [m for m in status_candidates if m not in chosen]
        if remaining_status:
            best_status = max(remaining_status, key=lambda m: m.get("accuracy") or 0)
            chosen.append(best_status)
            used_types.add(best_status.get("type"))

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


def type_multiplier_for(move_type: str, defender: "BattlePokemon") -> float:
    """Like type_multiplier(), but folds in ability-based type immunities
    (Levitate/Water Absorb/Volt Absorb/Flash Fire) so the AI's damage
    estimates never rate a move that would actually do nothing."""
    if ABILITY_IMMUNITY.get(getattr(defender, "ability", None)) == move_type:
        return 0.0
    return type_multiplier(move_type, defender.types)


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


# ── Battle scene image rendering ────────────────────────────────────────────

CANVAS_W, CANVAS_H = 720, 380
SPRITE_SCALE = 3

# Season x time-of-day background presets. One is rolled per battle (not per
# turn/render call) so the scenery stays consistent for the whole fight —
# see Battle.__init__ / self.background.
BACKGROUNDS = [
    dict(name="Spring Day",   sky=(135, 206, 235, 255), ground=(140, 200, 90, 255),
         patch=(160, 215, 110, 255), time="day",   season="spring"),
    dict(name="Spring Night", sky=(28, 38, 78, 255),    ground=(60, 85, 55, 255),
         patch=(75, 100, 68, 255),  time="night", season="spring"),
    dict(name="Summer Day",   sky=(90, 175, 240, 255),  ground=(120, 190, 70, 255),
         patch=(140, 205, 90, 255), time="day",   season="summer"),
    dict(name="Summer Night", sky=(18, 28, 66, 255),    ground=(45, 75, 48, 255),
         patch=(58, 90, 58, 255),   time="night", season="summer"),
    dict(name="Autumn Day",   sky=(180, 190, 210, 255), ground=(190, 140, 70, 255),
         patch=(205, 155, 85, 255), time="day",   season="autumn"),
    dict(name="Autumn Night", sky=(32, 32, 52, 255),    ground=(85, 65, 42, 255),
         patch=(98, 78, 52, 255),   time="night", season="autumn"),
    dict(name="Winter Day",   sky=(205, 222, 235, 255), ground=(232, 238, 245, 255),
         patch=(245, 248, 252, 255), time="day",  season="winter"),
    dict(name="Winter Night", sky=(12, 18, 42, 255),    ground=(195, 202, 215, 255),
         patch=(212, 218, 228, 255), time="night", season="winter"),
]


def pick_background() -> dict:
    """Roll a random season/time-of-day background preset for a battle.
    Stamps a fresh random seed onto the copy so the scattered decorations
    (stars/snow/leaves/flowers) are stable for every turn of *this* battle
    but still vary from the next battle that rolls the same preset."""
    preset = dict(random.choice(BACKGROUNDS))
    preset["seed"] = random.randint(0, 1_000_000_000)
    return preset

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


def _draw_background_flair(draw: "ImageDraw.ImageDraw", bg: dict, seed: int):
    """Season/time-of-day decorations layered on top of the sky+ground fill.
    `seed` keeps the scattered decorations (stars, snow, leaves...) stable
    across every render call for a given battle instead of jittering every
    turn, while still varying scene-to-scene."""
    rng = random.Random(seed)

    if bg["time"] == "night":
        # Moon, upper-left, plus a scatter of stars across the sky.
        mx, my, mr = int(CANVAS_W * 0.13), 46, 22
        draw.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(240, 240, 225, 255))
        draw.ellipse([mx - mr + 10, my - mr - 4, mx + mr + 10, my + mr - 4], fill=bg["sky"])
        for _ in range(28):
            sx = rng.randint(0, CANVAS_W)
            sy = rng.randint(0, CANVAS_H - 130)
            s = rng.choice((1, 1, 2))
            draw.ellipse([sx, sy, sx + s, sy + s], fill=(255, 255, 255, 220))
    else:
        # Sun, upper-right.
        sx, sy, sr = int(CANVAS_W * 0.88), 44, 26
        draw.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 235, 120, 255))

    if bg["season"] == "winter":
        for _ in range(40):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            s = rng.choice((2, 2, 3))
            draw.ellipse([fx, fy, fx + s, fy + s], fill=(255, 255, 255, 235))
    elif bg["season"] == "autumn":
        leaf_colors = [(200, 110, 40, 255), (215, 150, 40, 255), (170, 70, 30, 255)]
        for _ in range(22):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            draw.ellipse([fx, fy, fx + 4, fy + 4], fill=rng.choice(leaf_colors))
    elif bg["season"] == "spring":
        flower_colors = [(255, 255, 255, 255), (255, 200, 220, 255), (255, 230, 120, 255)]
        for _ in range(18):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            draw.ellipse([fx, fy, fx + 5, fy + 5], fill=rng.choice(flower_colors))


async def render_battle_scene(session: aiohttp.ClientSession,
                               opponent_pokemon: "BattlePokemon",
                               player_pokemon: "BattlePokemon",
                               background: Optional[dict] = None) -> Optional[discord.File]:
    """Classic side-on battle scene: opponent upper-right facing player,
    player's pokemon lower-left (mirrored) facing opponent, with an HP bar
    rendered directly above each sprite (name, Lv.100, colour-shifting bar).
    `background` is one of the presets in BACKGROUNDS (a season/time-of-day
    combo); if omitted, one is rolled on the spot."""
    if not PIL_OK:
        return None

    bg = background or pick_background()

    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg["sky"])
    draw = ImageDraw.Draw(canvas)
    _draw_background_flair(draw, bg, seed=bg.get("seed", 0))
    draw.rectangle([0, CANVAS_H - 120, CANVAS_W, CANVAS_H], fill=bg["ground"])
    draw.ellipse([CANVAS_W * 0.55 - 150, CANVAS_H - 150, CANVAS_W * 0.55 + 150, CANVAS_H - 90],
                 fill=bg["patch"])
    draw.ellipse([CANVAS_W * 0.14 - 120, CANVAS_H - 95, CANVAS_W * 0.14 + 120, CANVAS_H - 45],
                 fill=bg["patch"])

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
    eff = type_multiplier_for(move.get("type", "normal"), defender)
    if eff == 0:
        return 0.0
    base = (((2 * LEVEL / 5 + 2) * power * a / max(d, 1)) / 50 + 2)
    return base * stab * eff


SWITCH_HP_THRESHOLD = 0.25       # consider switching if best move clears less than this % of foe's HP
SWITCH_IMPROVEMENT_MARGIN = 0.15  # ...and only if a bench mon's matchup beats staying in by at least this much


def _accuracy_weighted_score(attacker: BattlePokemon, defender: BattlePokemon, move: dict) -> float:
    """Expected damage, discounted by the move's accuracy - a 150-power
    move that misses half the time should usually lose out to a reliable
    90-power move, not just whichever number is bigger on paper."""
    acc = move.get("accuracy")
    acc_frac = 1.0 if acc is None else acc / 100
    return estimate_damage(attacker, defender, move) * acc_frac


def _best_matchup_fraction(attacker: BattlePokemon, defender: BattlePokemon) -> float:
    """The attacker's single best accuracy-weighted move, as a fraction of
    the defender's max HP - used both to judge the AI's current matchup and
    to size up potential switch-in candidates. Moves with no PP left are
    skipped, same as a real trainer couldn't select them."""
    best = 0.0
    for m in attacker.moves:
        if m.get("current_pp", 1) <= 0:
            continue
        score = _accuracy_weighted_score(attacker, defender, m)
        if score > best:
            best = score
    return best / max(defender.max_hp, 1)


def bot_choose_action(trainer: Trainer, opponent: Trainer) -> tuple:
    """Battle AI for the bot's own trainer.

    Attacking: scores every move in its active Pokemon's move pool by
    accuracy-weighted expected damage (STAB, type effectiveness, and
    effective stats all factored in via estimate_damage), skips any move
    with no PP left, and attacks with the best one - or Struggles (index
    -1) if every move is out of PP.

    Switching: if the best available move would clear less than
    SWITCH_HP_THRESHOLD of the foe's HP, the bot looks at its bench. For
    each healthy teammate it compares "how hard would I hit them" against
    "how hard would they hit me back" (both accuracy-weighted, as a % of
    max HP) - if a teammate's net matchup beats staying in by more than
    SWITCH_IMPROVEMENT_MARGIN, the bot switches to it instead of attacking
    this turn. This is on top of the forced switches on faint handled by
    Battle.get_forced_switch."""
    active = trainer.active
    defender = opponent.active
    moves = active.moves

    available = [i for i, m in enumerate(moves) if m.get("current_pp", 1) > 0]
    if not available:
        best_idx = -1
        best_frac = 0.0
    else:
        scores = {i: _accuracy_weighted_score(active, defender, moves[i]) for i in available}
        best_idx = max(available, key=lambda i: scores[i])
        best_frac = scores[best_idx] / max(defender.max_hp, 1)

    bench = [(i, p) for i, p in enumerate(trainer.team) if not p.fainted and p is not active]
    if bench and best_frac < SWITCH_HP_THRESHOLD:
        stay_value = best_frac - _best_matchup_fraction(defender, active)
        best_switch_idx, best_switch_value = None, stay_value + SWITCH_IMPROVEMENT_MARGIN
        for i, candidate in bench:
            value = _best_matchup_fraction(candidate, defender) - _best_matchup_fraction(defender, candidate)
            if value > best_switch_value:
                best_switch_value = value
                best_switch_idx = i
        if best_switch_idx is not None:
            return ("switch", best_switch_idx)

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


# ── UI: post-battle rematch ──────────────────────────────────────────────────

class RematchView(discord.ui.View):
    """Posted after a battle ends. Against another human, BOTH trainers
    must click before the rematch starts; against the bot, only the human
    needs to. Reuses the exact format/team size/BST filter of the battle
    that just finished."""

    def __init__(self, cog: "BattleCog", channel: discord.TextChannel,
                 p1: discord.abc.User, p2: discord.abc.User,
                 fmt: str, count: int, bst_filter: tuple, vs_bot: bool):
        super().__init__(timeout=60)
        self.cog = cog
        self.channel = channel
        self.p1 = p1
        self.p2 = p2
        self.fmt = fmt
        self.count = count
        self.bst_filter = bst_filter
        self.vs_bot = vs_bot
        self.agreed: set = set()
        self.agreed_needed = {p1.id} if vs_bot else {p1.id, p2.id}
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="🔁 Rematch", style=discord.ButtonStyle.primary)
    async def rematch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.agreed_needed:
            await interaction.response.send_message(
                "Only the trainers from this battle can start a rematch.", ephemeral=True)
            return
        if self.channel.id in self.cog.pending or self.channel.id in self.cog.active_battles:
            await interaction.response.send_message(
                "There's already a challenge or battle active in this channel.", ephemeral=True)
            return
        self.agreed.add(interaction.user.id)
        if not self.agreed_needed <= self.agreed:
            await interaction.response.send_message(
                f"✅ {interaction.user.display_name} wants a rematch — waiting on the other trainer.",
            )
            return
        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await self.cog.start_rematch(self.channel, self.p1, self.p2,
                                      self.fmt, self.count, self.bst_filter, self.vs_bot)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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
        usable = [(i, mv) for i, mv in enumerate(trainer.active.moves) if mv.get("current_pp", 1) > 0]
        if not usable:
            # Out of PP on every move — the only legal action is Struggle.
            options.append(discord.SelectOption(
                label="Struggle",
                description="No PP left! Recoil damage to yourself.",
                value="-1",
            ))
        else:
            for i, mv in usable:
                tag = "⚡Priority • " if mv.get("priority", 0) > 0 else ""
                pp_txt = f"{mv.get('current_pp')}/{mv.get('pp') or '—'} PP"
                desc = f"{tag}{mv.get('type', 'normal').title()} • {mv.get('power') or '—'} power • {pp_txt}"
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
        self.timed_out_ids: set = set()

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
        # Fill in any missing action with that trainer's strongest move that
        # still has PP left (moves are pre-sorted by power) instead of a
        # random one — or Struggle (-1) if every move is out of PP.
        self.timed_out_ids = {
            trainer.user.id for trainer in (self.t1, self.t2)
            if trainer.user.id not in self.actions
        }
        for trainer in (self.t1, self.t2):
            if trainer.user.id not in self.actions:
                usable = [i for i, mv in enumerate(trainer.active.moves) if mv.get("current_pp", 1) > 0]
                idx = usable[0] if usable else -1
                self.actions[trainer.user.id] = ("move", idx)
                sel = self._select_for(trainer)
                sel.disabled = True
                fallback_label = "auto-used strongest move" if idx != -1 else "out of PP — used Struggle"
                sel.placeholder = f"{trainer.user.display_name} ran out of time — {fallback_label}"
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
                 t1: Trainer, t2: Trainer, fmt: str = "random", count: int = 3,
                 bst_filter: tuple = (None, None), vs_bot: bool = False):
        self.cog = cog
        self.channel = channel
        self.t1 = t1
        self.t2 = t2
        # Kept only so a "🔁 Rematch" button after the battle can recreate
        # the same matchup/format/team size without the trainers having to
        # retype the whole challenge.
        self.fmt = fmt
        self.count = count
        self.bst_filter = bst_filter
        self.vs_bot = vs_bot
        # Rolled once per battle (not per turn) so the scene stays the same
        # season/time-of-day for the whole fight instead of changing every
        # turn's image.
        self.background = pick_background()
        # Forfeit / AFK tracking.
        self.forfeited_trainer: Optional[Trainer] = None
        self.forfeit_reason: Optional[str] = None
        self.current_panel: Optional["BattlePanel"] = None
        self.afk_strikes: dict = {}

    async def build_embed(self, turn: int, last_summary: Optional[str],
                           final: bool = False, winner: Optional[Trainer] = None):
        file = await render_battle_scene(self.cog.session, self.t2.active, self.t1.active,
                                          background=self.background)

        embed = discord.Embed(
            title=("🏆 Battle Complete" if final else f"⚔️ Turn {turn}"),
            colour=(0xF1C40F if final else 0x3498DB),
        )
        if winner is not None:
            embed.description = f"**{winner.user.display_name} wins the battle!**"
        if last_summary:
            embed.add_field(name="📋 Last Turn's Results", value=last_summary[:1024], inline=False)

        for t in (self.t1, self.t2):
            if final:
                mon_lines = []
                for p in t.team:
                    marker = "💀" if p.fainted else "❤️"
                    item_suffix = f" 🎒 {item_label(p.item)}" if p.item else ""
                    mon_lines.append(f"{marker} {p.name.title()} — {p.hp}/{p.max_hp} HP{item_suffix}")
                embed.add_field(
                    name=t.user.display_name,
                    value="\n".join(mon_lines)[:1024],
                    inline=True,
                )
            else:
                p = t.active
                item_line = f"🎒 {item_label(p.item)}\n" if p.item else ""
                embed.add_field(
                    name=t.user.display_name,
                    value=(f"{p.name.title()} (Lv.{LEVEL})\n"
                           f"❤️ {p.hp}/{p.max_hp} HP\n"
                           f"{item_line}"
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
        """Resolves one move: accuracy check, ability immunities, damage
        (with a Sturdy check), status-move handling, recoil/drain, self-KO
        moves, secondary stat effects, and status ailments. Returns a list
        of flavor-text lines (usually 1-3) describing everything that
        happened."""
        move_name = move["name"].replace("-", " ").title()
        atk_mon, def_mon = attacker.active, defender.active

        acc = move.get("accuracy")
        if acc is not None and random.uniform(0, 100) > acc:
            return [f"❌ {atk_mon.name.title()}'s {move_name} missed!"]

        move_type = move.get("type", "normal")
        is_damaging = (move.get("power") or 0) > 0

        # Ability-based full type immunity (Levitate/Water Absorb/Volt
        # Absorb/Flash Fire) — the move does nothing (or heals the
        # defender, for the absorb abilities) and nothing else about it
        # resolves.
        if is_damaging and ABILITY_IMMUNITY.get(def_mon.ability) == move_type:
            ability_label = def_mon.ability.replace("-", " ").title()
            if def_mon.ability in ABILITY_ABSORB_HEAL:
                healed = min(def_mon.max_hp // 4, def_mon.max_hp - def_mon.hp)
                def_mon.hp += healed
                if healed > 0:
                    return [f"🛡️ {def_mon.name.title()}'s {ability_label} absorbed the attack and healed **{healed}** HP!"]
            return [f"🛡️ {def_mon.name.title()}'s {ability_label} makes {move_name} have no effect!"]

        lines = []
        dmg = 0
        if is_damaging:
            dmg, eff, crit = calc_damage(atk_mon, def_mon, move)
            sturdy_save = (def_mon.ability == "sturdy" and def_mon.hp == def_mon.max_hp
                           and dmg >= def_mon.hp)
            if sturdy_save:
                dmg = def_mon.hp - 1
            prev_def_hp = def_mon.hp
            def_mon.hp = max(0, def_mon.hp - dmg)
            actual_dealt = prev_def_hp - def_mon.hp

            text = f"➡️ {atk_mon.name.title()} used **{move_name}**! (**{dmg}** dmg)"
            if crit:
                text += " 💫 Critical hit!"
            if eff > 1:
                text += " It's super effective!"
            elif 0 < eff < 1:
                text += " It's not very effective..."
            elif eff == 0:
                text += " It had no effect!"
            lines.append(text)
            if sturdy_save:
                lines.append(f"🛡️ {def_mon.name.title()} hung on with Sturdy!")

            # Shell Bell: heals the attacker for a slice of the damage it
            # just dealt. Mild by design (1/8), and skipped if the
            # attacker fainted from its own recoil the same instant.
            if atk_mon.item == "shell-bell" and actual_dealt > 0 and not atk_mon.fainted:
                healed = min(max(1, actual_dealt // 8), atk_mon.max_hp - atk_mon.hp)
                if healed > 0:
                    atk_mon.hp += healed
                    lines.append(f"🔔 {atk_mon.name.title()}'s Shell Bell restored **{healed}** HP!")
        else:
            lines.append(f"➡️ {atk_mon.name.title()} used **{move_name}**!")

        # Self-KO moves: Explosion / Self-Destruct faint the user outright.
        if move["name"] in SELF_KO_MOVES:
            atk_mon.hp = 0
            lines.append(f"💥 {atk_mon.name.title()} was consumed by the blast!")
        elif move["name"] == "struggle":
            recoil = max(1, int(atk_mon.max_hp * STRUGGLE_RECOIL_FRACTION))
            atk_mon.hp = max(0, atk_mon.hp - recoil)
            lines.append(f"💥 {atk_mon.name.title()} is damaged by recoil!")
        else:
            recoil_msg = _apply_drain_recoil(atk_mon, dmg, move)
            if recoil_msg:
                lines.append(recoil_msg)

        lines.extend(_apply_secondary_effects(atk_mon, def_mon, move))

        status_msg = _apply_status_ailment(atk_mon, def_mon, move)
        if status_msg:
            lines.append(status_msg)

        return lines

    def _apply_switch_in_abilities(self, trainer: Trainer, opponent: Trainer) -> list:
        """Triggers on-switch-in ability effects for trainer's newly-active
        Pokemon (currently just Intimidate) against the opponent's current
        active. Returns flavor-text lines, if any."""
        mon = trainer.active
        lines = []
        if mon.ability == "intimidate" and not opponent.active.fainted:
            opp_mon = opponent.active
            old = opp_mon.stat_stages.get("atk", 0)
            opp_mon.stat_stages["atk"] = max(-6, old - 1)
            if opp_mon.stat_stages["atk"] != old:
                lines.append(f"😤 {mon.name.title()}'s Intimidate lowered {opp_mon.name.title()}'s Attack!")
        return lines

    def _check_berry(self, mon: BattlePokemon) -> list:
        """One-shot recovery berries (Oran/Sitrus) — trigger the first time
        a Pokemon drops to half HP or below, then are consumed."""
        if mon.fainted or mon.item_used or mon.item not in ("oran-berry", "sitrus-berry"):
            return []
        if mon.hp > mon.max_hp // 2:
            return []
        mon.item_used = True
        heal_frac = 8 if mon.item == "oran-berry" else 4
        healed = min(max(1, mon.max_hp // heal_frac), mon.max_hp - mon.hp)
        if healed <= 0:
            return []
        mon.hp += healed
        label = "Oran Berry" if mon.item == "oran-berry" else "Sitrus Berry"
        return [f"🍒 {mon.name.title()}'s {label} restored **{healed}** HP!"]

    def _apply_item_end_of_turn(self, mon: BattlePokemon) -> list:
        """Leftovers heals a little every end of turn; also re-checks the
        recovery berries in case status damage dropped a Pokemon below
        half HP this turn."""
        lines = []
        if mon.fainted:
            return lines
        if mon.item == "leftovers":
            healed = min(max(1, mon.max_hp // 16), mon.max_hp - mon.hp)
            if healed > 0:
                mon.hp += healed
                lines.append(f"🍃 {mon.name.title()}'s Leftovers restored **{healed}** HP!")
        lines.extend(self._check_berry(mon))
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

        # Intimidate can trigger from the very first send-out, same as
        # every later switch-in.
        start_lines = (self._apply_switch_in_abilities(self.t1, self.t2)
                       + self._apply_switch_in_abilities(self.t2, self.t1))
        if start_lines:
            await self.channel.send("\n".join(start_lines))

        turn = 1
        last_summary: Optional[str] = None
        pending_switches: list = []  # trainers whose active fainted last turn

        while self.t1.alive_team and self.t2.alive_team and self.forfeited_trainer is None:
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
                other = self.t2 if trainer is self.t1 else self.t1
                switch_in_lines = self._apply_switch_in_abilities(trainer, other)
                msg = f"{trainer.user.display_name} sent out **{trainer.active.name.title()}**!"
                if switch_in_lines:
                    msg += "\n" + "\n".join(switch_in_lines)
                await self.channel.send(msg)
            pending_switches = []

            # A forfeit may have landed while there was no panel open (e.g.
            # during the recap pause or a forced-switch prompt) — stop here
            # instead of dealing out a whole extra turn nobody will use.
            if self.forfeited_trainer is not None:
                break

            # 2) The actual actionable panel: image + trainer info + move
            #    dropdowns, same as before.
            panel = BattlePanel(self.t1, self.t2)
            self.current_panel = panel
            embed, file = await self.build_embed(turn, None)
            msg = await self._send_embed(
                embed, file,
                content=f"{self.t1.user.mention} {self.t2.user.mention} — choose your action.",
                view=panel,
            )
            panel.message = msg

            await panel.event.wait()
            self.current_panel = None

            # `!battle forfeit` may have fired while this turn's panel was
            # open — stop immediately rather than resolving the turn.
            if self.forfeited_trainer is not None:
                break

            # AFK tracking: a trainer who misses AFK_FORFEIT_STRIKES turns
            # in a row (no action submitted before TURN_TIMEOUT) auto-
            # forfeits instead of the bot auto-piloting them indefinitely.
            for trainer in (self.t1, self.t2):
                if trainer.is_bot:
                    continue
                if trainer.user.id in panel.timed_out_ids:
                    strikes = self.afk_strikes.get(trainer.user.id, 0) + 1
                    self.afk_strikes[trainer.user.id] = strikes
                    if strikes >= AFK_FORFEIT_STRIKES:
                        self.forfeited_trainer = trainer
                        self.forfeit_reason = "inactivity"
                else:
                    self.afk_strikes[trainer.user.id] = 0

            if self.forfeited_trainer is not None:
                await self.channel.send(
                    f"🏳️ {self.forfeited_trainer.user.display_name} missed "
                    f"{AFK_FORFEIT_STRIKES} turns in a row and auto-forfeited the battle."
                )
                break

            lines: list = []

            # 1) Switches resolve first and consume the whole turn for that
            #    trainer — a switched-in Pokemon never also attacks.
            for trainer, opponent in ((self.t1, self.t2), (self.t2, self.t1)):
                action = panel.actions.get(trainer.user.id)
                if action and action[0] == "switch":
                    old_name = trainer.active.name.title()
                    trainer.active_idx = action[1]
                    lines.append(
                        f"🔄 {trainer.user.display_name} withdrew {old_name} and sent out "
                        f"**{trainer.active.name.title()}**!"
                    )
                    lines.extend(self._apply_switch_in_abilities(trainer, opponent))
                elif action and action[0] == "pass":
                    lines.append(f"⏭️ {trainer.user.display_name} passed the turn.")

            # 2) Moves resolve in priority/speed order. Only trainers whose
            #    locked-in action was "move" attack this turn.
            movers = []
            for trainer, opponent in ((self.t1, self.t2), (self.t2, self.t1)):
                action = panel.actions.get(trainer.user.id)
                if action and action[0] == "move":
                    idx = action[1]
                    # idx is -1 (explicit Struggle) or points at a move
                    # that's since run out of PP (e.g. a stale queued
                    # action) — either way, Struggle. dict()-copy the
                    # fallback so its current_pp bookkeeping never mixes
                    # across Pokemon/turns.
                    if idx < 0 or idx >= len(trainer.active.moves) \
                            or trainer.active.moves[idx].get("current_pp", 1) <= 0:
                        move = dict(FALLBACK_MOVE)
                    else:
                        move = trainer.active.moves[idx]
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

                # Sleep/freeze/paralysis can prevent the move outright.
                can_move, status_line = _status_precheck(attacker.active)
                if status_line:
                    lines.append(status_line)
                if not can_move:
                    continue

                move_name = move["name"]
                if move_name != "struggle" and "current_pp" in move:
                    move["current_pp"] = max(0, move["current_pp"] - 1)
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

                # One-shot recovery berries can trigger off any HP loss
                # this move caused — the attacker's own recoil or the
                # defender taking damage.
                lines.extend(self._check_berry(attacker.active))
                lines.extend(self._check_berry(defender.active))

            # 3) End-of-turn residual status damage (burn/poison chip
            #    damage). Skipped for a Pokemon that already fainted this
            #    turn — matches the mainline games, no double-dipping.
            for trainer in (self.t1, self.t2):
                mon = trainer.active
                if mon.fainted:
                    continue
                if mon.status == "burn":
                    dmg = max(1, mon.max_hp // 16)
                    mon.hp = max(0, mon.hp - dmg)
                    lines.append(f"🔥 {mon.name.title()} is hurt by its burn! (**{dmg}** dmg)")
                elif mon.status == "poison":
                    dmg = max(1, mon.max_hp // 8)
                    mon.hp = max(0, mon.hp - dmg)
                    lines.append(f"☠️ {mon.name.title()} is hurt by poison! (**{dmg}** dmg)")
                if mon.fainted:
                    lines.append(f"💥 {mon.name.title()} fainted!")
                    if trainer.alive_team:
                        pending_switches.append(trainer)
                    continue
                # Leftovers heal / recovery-berry re-check for end-of-turn
                # status chip damage.
                lines.extend(self._apply_item_end_of_turn(mon))

            last_summary = "\n".join(lines) if lines else "No actions were taken."
            turn += 1

        if self.forfeited_trainer is not None:
            loser = self.forfeited_trainer
            winner = self.t2 if loser is self.t1 else self.t1
        else:
            # Battle's over the normal way — if the winner's own active
            # happened to faint on this final turn too (a mutual KO),
            # silently slot in their next healthy Pokemon so the final
            # embed doesn't show a fainted mon.
            for trainer in pending_switches:
                if trainer.alive_team:
                    for i, p in enumerate(trainer.team):
                        if not p.fainted:
                            trainer.active_idx = i
                            break

            winner = self.t1 if self.t1.alive_team else self.t2
            loser = self.t2 if winner is self.t1 else self.t1

        # Show the final turn's damage recap as its own embed first — same
        # as every other turn's flow — before the "Battle Complete" embed.
        # (Nothing to show on a forfeit before any turn resolved.)
        if last_summary and self.forfeited_trainer is None:
            await self.channel.send(embed=self.build_results_embed(last_summary))
            await asyncio.sleep(3)

        final_embed, final_file = await self.build_embed(turn, None,
                                                           final=True, winner=winner)
        await self._send_embed(final_embed, final_file)

        # Record win/loss for every human trainer in the battle (skip the
        # bot's own side — `!pf` tracks people, not the bot). vs_ai is True
        # whenever the opponent was the bot, so a `!battle @<bot>` fight
        # logs under "Vs AI" and a normal PvP fight logs under "Vs Humans"
        # for both participants.
        guild_id = self.channel.guild.id if self.channel.guild else 0
        vs_ai = self.t1.is_bot or self.t2.is_bot
        for trainer in (self.t1, self.t2):
            if trainer.is_bot:
                continue
            try:
                await _db.record_battle_result(guild_id, trainer.user.id, vs_ai, trainer is winner)
            except Exception:
                pass  # never let stat logging break the battle's end

        # Elo — rated for every battle, including vs the bot (the bot has
        # its own rating too, so `!elo lb` reflects how tough it's been).
        elo_note = ""
        try:
            new_w, w_delta, new_l, l_delta = await apply_elo_result(
                guild_id, winner.user.id, loser.user.id
            )
            elo_note = (
                f" ({winner.user.display_name} {'+' if w_delta >= 0 else ''}{w_delta} → **{new_w}**, "
                f"{loser.user.display_name} {l_delta} → **{new_l}**)"
            )
        except Exception:
            pass  # never let rating logging break the battle's end

        await self.channel.send(
            f"🏆 {winner.user.mention} wins the battle! GG {loser.user.display_name}.{elo_note}"
        )

        self.cog.active_battles.pop(self.channel.id, None)

        # Offer a quick rematch — same matchup, format, team size, and BST
        # filter as this battle. Only the two trainers involved can use it.
        if self.vs_bot:
            human = self.t2.user if self.t1.is_bot else self.t1.user
            rematch_view = RematchView(
                self.cog, self.channel, human, self.cog.bot.user,
                self.fmt, self.count, self.bst_filter, self.vs_bot,
            )
        else:
            rematch_view = RematchView(
                self.cog, self.channel, self.t1.user, self.t2.user,
                self.fmt, self.count, self.bst_filter, self.vs_bot,
            )
        rematch_view.message = await self.channel.send(
            "Want to go again?", view=rematch_view
        )


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
                      opponent: Optional[str] = None,
                      fmt: str = "random", count: int = 3,
                      *, bst_filter: Optional[str] = None):
        if opponent is None:
            await ctx.send(
                "Usage: `!battle @user [random|custom] [count 1-6] [>min<max]`\n"
                "The `>min<max` part is optional and filters `random` teams by "
                "base stat total, e.g. `!battle @user random 3 >590<700`. You "
                "can also battle me directly: `!battle ai [count] [>min<max]` "
                "(or `!battle @<my name> random 3 >550`)."
            )
            return

        if opponent.strip().lower() in ("ai", "bot", self.bot.user.name.lower()):
            opponent = self.bot.user
        else:
            try:
                opponent = await commands.MemberConverter().convert(ctx, opponent)
            except commands.BadArgument:
                await ctx.send(f"Couldn't find a member matching `{opponent}`. "
                                f"Try `!battle @user` or `!battle ai`.")
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
            battle = Battle(self, ctx.channel, t1, t2, fmt=fmt, count=count,
                             bst_filter=(min_total, max_total), vs_bot=True)
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
            battle = Battle(self, channel, t1, t2, fmt=fmt, count=count,
                             bst_filter=(min_total, max_total), vs_bot=False)
            self.active_battles[channel.id] = battle
            await battle.run()
        else:
            pending.accepted = True
            await channel.send(
                f"📋 Custom battle! Both trainers build your team with:\n"
                f"`!battle add pikachu, charizard, ...` (up to {count} each)\n"
                f"{challenger.mention} and {opponent.mention}, go ahead."
            )

    async def start_rematch(self, channel, p1, p2, fmt: str, count: int,
                             bst_filter: tuple, vs_bot: bool):
        """Recreates the exact matchup a `RematchView` button was clicked
        for. vs_bot always re-rolls immediately (mirrors the `!battle ai`
        shortcut); PvP reuses the same pending-challenge machinery as a
        fresh `!battle @user`, so random re-rolls immediately and custom
        re-opens the `!battle add` team-build phase."""
        if channel.id in self.pending or channel.id in self.active_battles:
            return

        if vs_bot:
            min_total, max_total = bst_filter
            filt_note = format_bst_filter(min_total, max_total)
            await channel.send(f"🔁 Rematch! Rolling random teams — {p1.mention} vs me!{filt_note}")
            t1 = Trainer(p1)
            t2 = Trainer(self.bot.user, is_bot=True)
            for _ in range(count):
                t1.team.append(await build_random_pokemon(self.session, min_total, max_total))
                t2.team.append(await build_random_pokemon(self.session, min_total, max_total))
            battle = Battle(self, channel, t1, t2, fmt=fmt, count=count,
                             bst_filter=bst_filter, vs_bot=True)
            self.active_battles[channel.id] = battle
            await battle.run()
            return

        await channel.send(f"🔁 Rematch! {p1.mention} vs {p2.mention}.")
        self.pending[channel.id] = PendingChallenge(p1, p2, fmt, count, bst_filter=bst_filter)
        await self.start_challenge(channel, p1, p2, fmt, count)

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
            battle = Battle(self, ctx.channel, t1, t2, fmt=pending.fmt, count=pending.count,
                             bst_filter=pending.bst_filter, vs_bot=False)
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

    @battle.command(name="forfeit")
    async def battle_forfeit(self, ctx: commands.Context):
        battle = self.active_battles.get(ctx.channel.id)
        if not battle:
            await ctx.send("There's no active battle in this channel to forfeit.")
            return

        trainer = None
        if battle.t1.user.id == ctx.author.id:
            trainer = battle.t1
        elif battle.t2.user.id == ctx.author.id:
            trainer = battle.t2

        if trainer is None:
            await ctx.send("You're not part of this battle.")
            return
        if trainer.is_bot:
            await ctx.send("The bot can't forfeit.")
            return
        if battle.forfeited_trainer is not None:
            await ctx.send("This battle is already wrapping up.")
            return

        battle.forfeited_trainer = trainer
        battle.forfeit_reason = "forfeit"
        await ctx.send(f"🏳️ {ctx.author.display_name} forfeits the battle!")

    @commands.command(name="bpf")
    async def battle_profile(self, ctx: commands.Context, *, target: Optional[str] = None):
        """!pf [@user] — shows a trainer's battle record (defaults to you),
        split into PvP results and results against the bot.
        !pf ai — shows the BOT's own global record against everyone in
        this server (the flip side of everyone's individual Vs AI stats)."""
        guild_id = ctx.guild.id if ctx.guild else 0

        if target and target.strip().lower() in ("ai", "bot", self.bot.user.name.lower()):
            stats = await _db.get_ai_global_stats(guild_id)
            embed = discord.Embed(
                title=f"🤖 {self.bot.user.display_name}'s Battle Record (vs. everyone)",
                colour=0xE67E22,
            )
            embed.add_field(
                name="Vs All Trainers",
                value=(f"Total battles: **{stats['total']}**\n"
                       f"Win: **{stats['ai_wins']}**\n"
                       f"Loss: **{stats['ai_losses']}**"),
                inline=False,
            )
            await ctx.send(embed=embed)
            return

        member = ctx.author
        if target:
            try:
                member = await commands.MemberConverter().convert(ctx, target)
            except commands.BadArgument:
                await ctx.send(f"Couldn't find a member matching `{target}`. "
                                f"Try `!pf @user` or `!pf ai`.")
                return

        stats = await _db.get_battle_stats(guild_id, member.id)

        embed = discord.Embed(
            title=f"⚔️ {member.display_name}'s Battle Record",
            colour=0x3498DB,
        )
        embed.add_field(
            name="Vs Humans",
            value=(f"Total battles: **{stats['human_total']}**\n"
                   f"Win: **{stats['human_wins']}**\n"
                   f"Loss: **{stats['human_losses']}**"),
            inline=True,
        )
        embed.add_field(
            name="Vs AI",
            value=(f"Total battles: **{stats['ai_total']}**\n"
                   f"Win: **{stats['ai_wins']}**\n"
                   f"Loss: **{stats['ai_losses']}**"),
            inline=True,
        )
        await ctx.send(embed=embed)

    @commands.group(name="elo", invoke_without_command=True)
    async def elo(self, ctx: commands.Context, *, target: Optional[str] = None):
        """!elo [@user] — shows a trainer's Elo battle rating (starts at
        1000, rated for every battle including vs the bot)."""
        guild_id = ctx.guild.id if ctx.guild else 0

        member = ctx.author
        if target and target.strip().lower() not in ("", ):
            if target.strip().lower() in ("ai", "bot", self.bot.user.name.lower()):
                member = self.bot.user
            else:
                try:
                    member = await commands.MemberConverter().convert(ctx, target)
                except commands.BadArgument:
                    await ctx.send(f"Couldn't find a member matching `{target}`. "
                                    f"Try `!elo @user` or `!elo lb`.")
                    return

        rating = await _get_elo(guild_id, member.id)
        embed = discord.Embed(
            title=f"📊 {member.display_name}'s Elo Rating",
            description=f"**{rating}**",
            colour=0x9B59B6,
        )
        await ctx.send(embed=embed)

    @elo.command(name="elolb")
    async def elo_leaderboard(self, ctx: commands.Context):
        """!elo lb — shows the server's Elo leaderboard (top 10), bot included."""
        guild_id = ctx.guild.id if ctx.guild else 0
        cursor = _col().battle_elo.find({"guild_id": guild_id}).sort("elo", -1).limit(10)
        docs = [doc async for doc in cursor]

        if not docs:
            await ctx.send("No rated battles have been played in this server yet.")
            return

        lines = []
        for i, doc in enumerate(docs, start=1):
            user_id = doc["user_id"]
            if ctx.guild:
                member = ctx.guild.get_member(user_id)
            else:
                member = None
            name = member.display_name if member else (
                self.bot.user.display_name if user_id == self.bot.user.id else f"<@{user_id}>"
            )
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`#{i}`")
            lines.append(f"{medal} {name} — **{doc['elo']}**")

        embed = discord.Embed(
            title="📊 Elo Leaderboard",
            description="\n".join(lines),
            colour=0x9B59B6,
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))
