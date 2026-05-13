"""
db.py  —  MongoDB helpers for the Pokémon tracker bot.

Data is kept PERMANENTLY — no TTL expiry.
  • "Today" queries filter by >= start of current UTC day (midnight).
  • All-time queries have no timestamp filter.

Collections
-----------
catches   : one doc per catch event
  guild_id, user_id, pokemon, iv, shiny, gigantamax, chain_shiny,
  channel_id, timestamp

flees     : one doc per flee event
  guild_id, pokemon, channel_id, timestamp

fled_log_channels : guild + category → log channel routing
  guild_id, category_key, channel_id
"""

import os
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME   = os.getenv("MONGO_DB",  "pokebot")

_client: AsyncIOMotorClient | None = None


def get_db():
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI)
    return _client[DB_NAME]


def _today_start() -> datetime:
    """UTC midnight of the current day."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _today_reset_unix() -> int:
    """Unix timestamp of the next UTC midnight (when today's window resets)."""
    tomorrow = _today_start() + timedelta(days=1)
    return int(tomorrow.timestamp())


def today_label() -> str:
    """e.g. '2025-07-14'"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── One-time index setup (call at startup) ────────────────────────────────────

    """
    db_additions.py
    ───────────────
    Paste the `ensure_indexes` replacement into db.py,
    replacing the existing function.

    No other changes to db.py are needed — all Mongo calls for
    autopause_config and locked_channels live directly in autopause_cog.py.
    """

    # ── Replace the existing ensure_indexes() in db.py with this ─────────────────

"""
db_additions.py
───────────────
Paste the `ensure_indexes` replacement into db.py,
replacing the existing function.

No other changes to db.py are needed — all Mongo calls for
autopause_config and locked_channels live directly in autopause_cog.py.
"""

# ── Replace the existing ensure_indexes() in db.py with this ─────────────────

async def ensure_indexes() -> None:
    """
    Create query indexes.
    NOTE: No TTL index — data is kept forever.
    If you previously had a TTL index on catches/flees, drop it manually:
      db.catches.dropIndex("timestamp_1")
      db.flees.dropIndex("timestamp_1")
    """
    db = get_db()
    await db.catches.create_index([("guild_id", 1), ("timestamp", -1)])
    await db.catches.create_index([("guild_id", 1), ("user_id",   1), ("timestamp", -1)])
    await db.flees.create_index(  [("guild_id", 1), ("timestamp", -1)])

    # Autopause indexes
    await db.autopause_config.create_index([("guild_id", 1)], unique=True)
    await db.locked_channels.create_index( [("guild_id", 1), ("channel_id", 1)], unique=True)

# ── Catches ───────────────────────────────────────────────────────────────────

async def record_catch(
    guild_id:    int,
    user_id:     int,
    pokemon:     str,
    iv:          float | None,
    shiny:       bool,
    gigantamax:  bool,
    chain_shiny: bool,
    channel_id:  int,
) -> None:
    await get_db().catches.insert_one({
        "guild_id":    guild_id,
        "user_id":     user_id,
        "pokemon":     pokemon,
        "iv":          iv,
        "shiny":       shiny,
        "gigantamax":  gigantamax,
        "chain_shiny": chain_shiny,
        "channel_id":  channel_id,
        "timestamp":   datetime.now(timezone.utc),
    })


# ── Flees ─────────────────────────────────────────────────────────────────────

async def record_flee(guild_id: int, pokemon: str, channel_id: int) -> None:
    await get_db().flees.insert_one({
        "guild_id":   guild_id,
        "pokemon":    pokemon,
        "channel_id": channel_id,
        "timestamp":  datetime.now(timezone.utc),
    })


# ── Reset-time helper ─────────────────────────────────────────────────────────

async def get_window_reset_info(guild_id: int) -> dict:
    """
    Returns info about today's window:
      reset_unix  : int    — Unix timestamp of next UTC midnight
      today_label : str    — e.g. '2025-07-14'
    """
    return {
        "reset_unix":  _today_reset_unix(),
        "today_label": today_label(),
    }


# ── User stats (today + all-time) ─────────────────────────────────────────────

async def get_user_stats(guild_id: int, user_id: int) -> dict:
    """Returns catch stats for today (UTC midnight → now)."""
    db    = get_db()
    since = _today_start()
    pipeline = [
        {"$match": {"guild_id": guild_id, "user_id": user_id, "timestamp": {"$gte": since}}},
        {"$group": {
            "_id":         None,
            "total":       {"$sum": 1},
            "shiny":       {"$sum": {"$cond": ["$shiny",       1, 0]}},
            "gigantamax":  {"$sum": {"$cond": ["$gigantamax",  1, 0]}},
            "chain_shiny": {"$sum": {"$cond": ["$chain_shiny", 1, 0]}},
        }},
    ]
    result = await db.catches.aggregate(pipeline).to_list(1)
    if not result:
        return {"total": 0, "shiny": 0, "gigantamax": 0, "chain_shiny": 0}
    r = result[0]
    r.pop("_id", None)
    return r


async def get_user_stats_alltime(guild_id: int, user_id: int) -> dict:
    """Returns all-time catch stats (no time filter)."""
    db = get_db()
    pipeline = [
        {"$match": {"guild_id": guild_id, "user_id": user_id}},
        {"$group": {
            "_id":         None,
            "total":       {"$sum": 1},
            "shiny":       {"$sum": {"$cond": ["$shiny",       1, 0]}},
            "gigantamax":  {"$sum": {"$cond": ["$gigantamax",  1, 0]}},
            "chain_shiny": {"$sum": {"$cond": ["$chain_shiny", 1, 0]}},
        }},
    ]
    result = await db.catches.aggregate(pipeline).to_list(1)
    if not result:
        return {"total": 0, "shiny": 0, "gigantamax": 0, "chain_shiny": 0}
    r = result[0]
    r.pop("_id", None)
    return r


async def get_user_pokemon_list(guild_id: int, user_id: int) -> list[dict]:
    """Returns [{pokemon, count}, ...] sorted descending by count, today only."""
    db    = get_db()
    since = _today_start()
    pipeline = [
        {"$match": {"guild_id": guild_id, "user_id": user_id, "timestamp": {"$gte": since}}},
        {"$group": {"_id": "$pokemon", "count": {"$sum": 1}}},
        {"$sort":  {"count": -1}},
        {"$project": {"pokemon": "$_id", "count": 1, "_id": 0}},
    ]
    return await db.catches.aggregate(pipeline).to_list(None)


async def get_user_pokemon_list_alltime(guild_id: int, user_id: int) -> list[dict]:
    """Returns [{pokemon, count}, ...] sorted descending by count, all time."""
    db = get_db()
    pipeline = [
        {"$match": {"guild_id": guild_id, "user_id": user_id}},
        {"$group": {"_id": "$pokemon", "count": {"$sum": 1}}},
        {"$sort":  {"count": -1}},
        {"$project": {"pokemon": "$_id", "count": 1, "_id": 0}},
    ]
    return await db.catches.aggregate(pipeline).to_list(None)


# ── Category stats (today + all-time) ─────────────────────────────────────────

async def get_category_stats(guild_id: int, category_pokemon: set[str]) -> dict:
    """Returns {caught, fled, total_spawned} for a set of Pokémon, today only."""
    db    = get_db()
    since = _today_start()
    plist = list(category_pokemon)

    caught = await db.catches.count_documents({
        "guild_id":  guild_id,
        "pokemon":   {"$in": plist},
        "timestamp": {"$gte": since},
    })
    fled = await db.flees.count_documents({
        "guild_id":  guild_id,
        "pokemon":   {"$in": plist},
        "timestamp": {"$gte": since},
    })
    return {"caught": caught, "fled": fled, "total_spawned": caught + fled}


async def get_category_stats_alltime(guild_id: int, category_pokemon: set[str]) -> dict:
    """Returns {caught, fled, total_spawned} for a set of Pokémon, all time."""
    db    = get_db()
    plist = list(category_pokemon)

    caught = await db.catches.count_documents({
        "guild_id": guild_id,
        "pokemon":  {"$in": plist},
    })
    fled = await db.flees.count_documents({
        "guild_id": guild_id,
        "pokemon":  {"$in": plist},
    })
    return {"caught": caught, "fled": fled, "total_spawned": caught + fled}


# ── Leaderboard (today + all-time) ────────────────────────────────────────────

async def get_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    """Global catch leaderboard for today (UTC)."""
    db    = get_db()
    since = _today_start()
    pipeline = [
        {"$match": {"guild_id": guild_id, "timestamp": {"$gte": since}}},
        {"$group": {
            "_id":        "$user_id",
            "total":      {"$sum": 1},
            "shiny":      {"$sum": {"$cond": ["$shiny",      1, 0]}},
            "gigantamax": {"$sum": {"$cond": ["$gigantamax", 1, 0]}},
        }},
        {"$sort":  {"total": -1}},
        {"$limit": limit},
    ]
    docs = await db.catches.aggregate(pipeline).to_list(limit)
    return [{"user_id": d["_id"], **{k: d[k] for k in ("total", "shiny", "gigantamax")}} for d in docs]


async def get_leaderboard_alltime(guild_id: int, limit: int = 10) -> list[dict]:
    """Global catch leaderboard — all time."""
    db = get_db()
    pipeline = [
        {"$match": {"guild_id": guild_id}},
        {"$group": {
            "_id":        "$user_id",
            "total":      {"$sum": 1},
            "shiny":      {"$sum": {"$cond": ["$shiny",      1, 0]}},
            "gigantamax": {"$sum": {"$cond": ["$gigantamax", 1, 0]}},
        }},
        {"$sort":  {"total": -1}},
        {"$limit": limit},
    ]
    docs = await db.catches.aggregate(pipeline).to_list(limit)
    return [{"user_id": d["_id"], **{k: d[k] for k in ("total", "shiny", "gigantamax")}} for d in docs]


async def get_category_leaderboard(
    guild_id: int, category_pokemon: set[str], limit: int = 10
) -> list[dict]:
    """Category catch leaderboard for today (UTC)."""
    db    = get_db()
    since = _today_start()
    pipeline = [
        {"$match": {
            "guild_id":  guild_id,
            "pokemon":   {"$in": list(category_pokemon)},
            "timestamp": {"$gte": since},
        }},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort":  {"total": -1}},
        {"$limit": limit},
    ]
    docs = await db.catches.aggregate(pipeline).to_list(limit)
    return [{"user_id": d["_id"], "total": d["total"]} for d in docs]


async def get_category_leaderboard_alltime(
    guild_id: int, category_pokemon: set[str], limit: int = 10
) -> list[dict]:
    """Category catch leaderboard — all time."""
    db = get_db()
    pipeline = [
        {"$match": {
            "guild_id": guild_id,
            "pokemon":  {"$in": list(category_pokemon)},
        }},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort":  {"total": -1}},
        {"$limit": limit},
    ]
    docs = await db.catches.aggregate(pipeline).to_list(limit)
    return [{"user_id": d["_id"], "total": d["total"]} for d in docs]


# ── Shiny leaderboard (today + all-time) ──────────────────────────────────────

async def get_shiny_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    """Shiny catch leaderboard for today (UTC)."""
    db    = get_db()
    since = _today_start()
    pipeline = [
        {"$match": {
            "guild_id":  guild_id,
            "timestamp": {"$gte": since},
            "$or": [{"shiny": True}, {"chain_shiny": True}],
        }},
        {"$group": {
            "_id":         "$user_id",
            "shiny":       {"$sum": {"$cond": ["$shiny",       1, 0]}},
            "chain_shiny": {"$sum": {"$cond": ["$chain_shiny", 1, 0]}},
            "total":       {"$sum": 1},
        }},
        {"$sort":  {"total": -1}},
        {"$limit": limit},
    ]
    docs = await db.catches.aggregate(pipeline).to_list(limit)
    return [
        {"user_id": d["_id"], "total": d["total"],
         "shiny": d["shiny"], "chain_shiny": d["chain_shiny"]}
        for d in docs
    ]


async def get_shiny_leaderboard_alltime(guild_id: int, limit: int = 10) -> list[dict]:
    """Shiny catch leaderboard — all time."""
    db = get_db()
    pipeline = [
        {"$match": {
            "guild_id": guild_id,
            "$or": [{"shiny": True}, {"chain_shiny": True}],
        }},
        {"$group": {
            "_id":         "$user_id",
            "shiny":       {"$sum": {"$cond": ["$shiny",       1, 0]}},
            "chain_shiny": {"$sum": {"$cond": ["$chain_shiny", 1, 0]}},
            "total":       {"$sum": 1},
        }},
        {"$sort":  {"total": -1}},
        {"$limit": limit},
    ]
    docs = await db.catches.aggregate(pipeline).to_list(limit)
    return [
        {"user_id": d["_id"], "total": d["total"],
         "shiny": d["shiny"], "chain_shiny": d["chain_shiny"]}
        for d in docs
    ]


# ── Gigantamax leaderboard (today + all-time) ─────────────────────────────────

async def get_gigantamax_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    """Gigantamax catch leaderboard for today (UTC)."""
    db    = get_db()
    since = _today_start()
    pipeline = [
        {"$match": {
            "guild_id":   guild_id,
            "timestamp":  {"$gte": since},
            "gigantamax": True,
        }},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort":  {"total": -1}},
        {"$limit": limit},
    ]
    docs = await db.catches.aggregate(pipeline).to_list(limit)
    return [{"user_id": d["_id"], "total": d["total"]} for d in docs]


async def get_gigantamax_leaderboard_alltime(guild_id: int, limit: int = 10) -> list[dict]:
    """Gigantamax catch leaderboard — all time."""
    db = get_db()
    pipeline = [
        {"$match": {"guild_id": guild_id, "gigantamax": True}},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort":  {"total": -1}},
        {"$limit": limit},
    ]
    docs = await db.catches.aggregate(pipeline).to_list(limit)
    return [{"user_id": d["_id"], "total": d["total"]} for d in docs]


# ── Fled-log channel config ────────────────────────────────────────────────────

async def set_fled_log_channel(guild_id: int, category_key: str, channel_id: int) -> None:
    await get_db().fled_log_channels.update_one(
        {"guild_id": guild_id, "category_key": category_key},
        {"$set": {"channel_id": channel_id}},
        upsert=True,
    )


async def get_fled_log_channels(guild_id: int) -> list[dict]:
    return await get_db().fled_log_channels.find({"guild_id": guild_id}).to_list(None)


async def get_fled_log_channel(guild_id: int, category_key: str) -> int | None:
    doc = await get_db().fled_log_channels.find_one(
        {"guild_id": guild_id, "category_key": category_key}
    )
    return doc["channel_id"] if doc else None


# ── Data management ───────────────────────────────────────────────────────────

async def clear_guild_data(guild_id: int) -> dict:
    """
    [Owner only] Delete ALL catches and flees for a guild (permanent).
    Returns {"catches": int, "flees": int} with the deleted counts.
    """
    db = get_db()
    catch_result = await db.catches.delete_many({"guild_id": guild_id})
    flee_result  = await db.flees.delete_many({"guild_id": guild_id})
    return {
        "catches": catch_result.deleted_count,
        "flees":   flee_result.deleted_count,
    }
