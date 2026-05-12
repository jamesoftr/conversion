"""
db.py  —  MongoDB helpers for the Pokémon tracker bot.

Data is kept PERMANENTLY — no TTL expiry.
  • "Last 24 h" queries are filtered in Python/Mongo with a $gte timestamp.
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

MONGO_URI  = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME    = os.getenv("MONGO_DB",  "pokebot")
WINDOW_H   = 24   # hours for the "last 24 h" rolling window

_client: AsyncIOMotorClient | None = None


def get_db():
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI)
    return _client[DB_NAME]


def _since() -> datetime:
    """UTC timestamp for WINDOW_H hours ago."""
    return datetime.now(timezone.utc) - timedelta(hours=WINDOW_H)


# ── One-time index setup (call at startup) ────────────────────────────────────

async def ensure_indexes() -> None:
    """
    Create query indexes.
    NOTE: No TTL index — data is kept forever.
    If you previously had a TTL index on catches/flees, drop it manually:
      db.catches.dropIndex("timestamp_1")
      db.flees.dropIndex("timestamp_1")
    """
    db = get_db()
    # NO expireAfterSeconds — data is permanent
    await db.catches.create_index([("guild_id", 1), ("timestamp", -1)])
    await db.catches.create_index([("guild_id", 1), ("user_id",   1), ("timestamp", -1)])
    await db.flees.create_index(  [("guild_id", 1), ("timestamp", -1)])


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
    Returns info about the rolling 24-hour window:
      oldest_in_window : datetime | None  — timestamp of the oldest catch/flee in the window
      resets_in_h      : float            — hours until that record drops out of the window
                                            (i.e. oldest + 24h - now). 0 if window is empty.
    """
    db    = get_db()
    since = _since()
    now   = datetime.now(timezone.utc)

    # Oldest catch in the current 24-h window
    cursor = db.catches.find(
        {"guild_id": guild_id, "timestamp": {"$gte": since}},
        {"timestamp": 1}
    ).sort("timestamp", 1).limit(1)
    docs = await cursor.to_list(1)

    if not docs:
        # Also check flees
        cursor = db.flees.find(
            {"guild_id": guild_id, "timestamp": {"$gte": since}},
            {"timestamp": 1}
        ).sort("timestamp", 1).limit(1)
        docs = await cursor.to_list(1)

    if not docs:
        return {"oldest_in_window": None, "resets_in_h": 0.0}

    oldest = docs[0]["timestamp"]
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)

    resets_at  = oldest + timedelta(hours=WINDOW_H)
    resets_in  = max(0.0, (resets_at - now).total_seconds() / 3600)
    return {"oldest_in_window": oldest, "resets_in_h": resets_in}


def fmt_reset(resets_in_h: float) -> str:
    """Human-readable 'resets in Xh Ym' string."""
    if resets_in_h <= 0:
        return "window is empty"
    total_min = int(resets_in_h * 60)
    h, m = divmod(total_min, 60)
    if h and m:
        return f"resets in {h}h {m}m"
    if h:
        return f"resets in {h}h"
    return f"resets in {m}m"


# ── User stats (last 24 h) ────────────────────────────────────────────────────

async def get_user_stats(guild_id: int, user_id: int) -> dict:
    """Returns catch stats for the last 24 h."""
    db    = get_db()
    since = _since()
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
    """Returns [{pokemon, count}, ...] sorted descending by count, last 24 h."""
    db    = get_db()
    since = _since()
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


# ── Category stats (last 24 h + all-time) ─────────────────────────────────────

async def get_category_stats(guild_id: int, category_pokemon: set[str]) -> dict:
    """Returns {caught, fled, total_spawned} for a set of Pokémon, last 24 h."""
    db    = get_db()
    since = _since()
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


# ── Leaderboard (last 24 h + all-time) ───────────────────────────────────────

async def get_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    """Global catch leaderboard for the last 24 h."""
    db    = get_db()
    since = _since()
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
    """Category catch leaderboard for the last 24 h."""
    db    = get_db()
    since = _since()
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
    [Owner only] Delete ALL catches and flees for a guild (permanent — no time filter).
    Returns {"catches": int, "flees": int} with the deleted counts.
    """
    db = get_db()

    catch_result = await db.catches.delete_many({"guild_id": guild_id})
    flee_result  = await db.flees.delete_many({"guild_id": guild_id})
    return {
        "catches": catch_result.deleted_count,
        "flees":   flee_result.deleted_count,
    }
