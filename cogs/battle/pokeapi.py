"""
cogs/battle/pokeapi.py
────────────────────────
PokeAPI fetch + MongoDB cache layer, move selection, and random-team
building.

Performance notes
------------------
Two things used to make `random` team rolls slow (multi-minute first
embeds, and still slow hours after startup even with a warm cache):

1. `pick_moves()` looked up every move in a Pokemon's move pool with an
   individual `find_one` each — a Pokemon with an 80-move learnset meant
   80 separate Mongo round trips just to check the cache, every single
   time, cache-warm or not. `get_move_data_bulk()` below replaces that
   with ONE `$in` query for everything already cached, and only falls
   back to individual PokeAPI fetches (still concurrent) for genuinely
   new moves.
2. Building a team rolled each Pokemon one at a time (`for _ in
   range(count): team.append(await build_random_pokemon(...))`), and
   both trainers' teams were built sequentially after each other too.
   For a 3v3 that's up to 6 fully-serial builds; for a 6v6, up to 12 —
   each one waiting on the previous to finish even when nothing about
   them depends on each other. `build_team()` fixes this by rolling all
   of one side's Pokemon concurrently with `asyncio.gather`; the cog
   then also gathers both trainers' `build_team()` calls together, so a
   6v6 does all 12 rolls in parallel instead of one after another.
"""

import asyncio
import random
import re
from typing import Optional

import aiohttp

from .constants import (
    POKEAPI, FALLBACK_MOVE, PRIORITY_MOVE_MIN_POWER, STAB_MOVE_MIN_POWER,
    STATUS_INDUCING_AILMENTS, _col,
)
from .engine import BattlePokemon


# ── PokeAPI fetch + Mongo cache ─────────────────────────────────────────────

# Bumped whenever the shape of a cached move document changes (e.g. adding
# stat_changes/drain support). A cached doc whose "_schema" doesn't match
# is treated as a miss and re-fetched — otherwise a move cached under an
# older schema (missing fields a newer feature relies on, like stat drops
# or recoil) would silently keep returning incomplete data forever, since
# get_move_data() would otherwise trust any cache hit unconditionally.
MOVE_CACHE_SCHEMA = 2


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
    if doc and doc.get("_schema") == MOVE_CACHE_SCHEMA:
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
        "_schema": MOVE_CACHE_SCHEMA,
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


async def get_move_data_bulk(session: aiohttp.ClientSession, names: list) -> dict:
    """Like get_move_data(), but for a whole list of move names at once.
    Fetches every already-cached move in a SINGLE Mongo query instead of
    one `find_one` per move, then only hits PokeAPI (concurrently) for
    the ones that weren't cached yet. Returns {name: move_dict}."""
    keys = list({n.lower().strip() for n in names})
    if not keys:
        return {}

    found = {}
    async for doc in _col().move_cache.find({"_id": {"$in": keys}, "_schema": MOVE_CACHE_SCHEMA}):
        found[doc["_id"]] = doc

    missing = [k for k in keys if k not in found]
    if missing:
        fetched = await asyncio.gather(
            *[get_move_data(session, name) for name in missing],
            return_exceptions=True,
        )
        for name, mv in zip(missing, fetched):
            if isinstance(mv, dict):
                found[name] = mv

    return found


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

    Same idea again for pure stat-change status moves (Swords Dance, Nasty
    Plot, Growl, Leer, ...): if the Pokemon learns one, the one with the
    largest total stat swing gets a slot, so boosting/dropping stats isn't
    limited to whatever a damaging move happens to do as a side effect.
    """
    pool = data.get("move_pool", [])
    if not pool:
        tackle = await get_move_data(session, "tackle")
        return [tackle] if tackle else [FALLBACK_MOVE]

    move_map = await get_move_data_bulk(session, pool)
    results = list(move_map.values())
    candidates = [
        mv for mv in results
        if mv.get("power") and mv.get("damage_class") != "status"
    ]
    status_candidates = [
        mv for mv in results
        if mv.get("damage_class") == "status"
        and mv.get("ailment") in STATUS_INDUCING_AILMENTS
        and not mv.get("ailment_chance")  # only the move's own guaranteed effect, not a % secondary
    ]
    stat_status_candidates = [
        mv for mv in results
        if mv.get("damage_class") == "status" and mv.get("stat_changes")
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

    # Stat-change guarantee: one status move that boosts the user or drops
    # the target's stats (Swords Dance, Nasty Plot, Growl, Leer, ...), if
    # there's still room. Picked by total magnitude of stat change so a
    # move like Swords Dance (+2) or Nasty Plot (+2) beats a mild +1.
    if len(chosen) < count and stat_status_candidates:
        remaining_stat_status = [m for m in stat_status_candidates if m not in chosen]
        if remaining_stat_status:
            def _stat_change_score(m):
                return sum(abs(sc.get("change", 0)) for sc in m.get("stat_changes", []))
            best_stat_status = max(remaining_stat_status, key=_stat_change_score)
            chosen.append(best_stat_status)
            used_types.add(best_stat_status.get("type"))

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
# Pokemon battles with the same fixed stat calc (see constants._calc_stat).

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
        # this automatically) so future rolls find matches i nstantly via
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


async def build_team(session: aiohttp.ClientSession, count: int,
                      min_total: Optional[int] = None,
                      max_total: Optional[int] = None) -> list:
    """Rolls a full `count`-Pokemon team concurrently instead of one Pokemon
    at a time — see the module docstring for why this matters."""
    return list(await asyncio.gather(
        *[build_random_pokemon(session, min_total, max_total) for _ in range(count)]
    ))
