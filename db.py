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

import math
import os
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

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
    # Date-bucket indexes for fast per-day queries
    await db.catches.create_index([("guild_id", 1), ("user_id", 1), ("date", 1)])
    await db.catches.create_index([("guild_id", 1), ("date", 1)])
    await db.flees.create_index(  [("guild_id", 1), ("timestamp", -1)])

    # Catch dedup — prevents double-recording the same catch message
    await db.catch_seen_messages.create_index([("message_id", 1)], unique=True)

    # Autopause indexes
    await db.autopause_config.create_index([("guild_id", 1)], unique=True)
    await db.locked_channels.create_index( [("guild_id", 1), ("channel_id", 1)], unique=True)

    await db.custom_pokemon_lists.create_index([("guild_id", 1)], unique=True)

    # Element quiz leaderboard index
    await db.quiz_scores.create_index(
        [("scope_key", 1), ("user_id", 1)], unique=True
    )

    # Loans collection indexes
    await db.loans.create_index([("guild_id", 1), ("lender_id",   1)])
    await db.loans.create_index([("guild_id", 1), ("borrower_id", 1)])
    await db.loans.create_index([("loan_id",  1)], unique=True)

    # Box tracker index — one doc per user per day per guild
    await db.box_openings.create_index(
        [("guild_id", 1), ("user_id", 1), ("date", 1)], unique=True
    )

    # Dedup index — prevents recording the same embed message twice
    await db.box_seen_messages.create_index(
        [("message_id", 1)], unique=True
    )

    # Welcome cog — one config doc per guild
    await db.welcome_config.create_index([("guild_id", 1)], unique=True)

    # Battle cog — win/loss records, one doc per human trainer per battle
    await db.battle_results.create_index([("guild_id", 1), ("user_id", 1)])

# ── Catches ───────────────────────────────────────────────────────────────────

async def mark_catch_message_seen(message_id: int) -> bool:
    """
    Insert message_id into the catch seen-set.
    Returns True if newly inserted (safe to record), False if duplicate (skip).
    Race-safe via unique index + DuplicateKeyError.
    """
    from pymongo.errors import DuplicateKeyError
    try:
        await get_db().catch_seen_messages.insert_one({"message_id": message_id})
        return True
    except DuplicateKeyError:
        return False


async def record_catch(
    guild_id:    int,
    user_id:     int,
    pokemon:     str,
    iv:          float | None,
    shiny:       bool,
    gigantamax:  bool,
    chain_shiny: bool,
    channel_id:  int,
    message_id:  int | None = None,
) -> bool:
    """
    Record a catch event. Returns True if recorded, False if duplicate message.
    Pass message_id to enable dedup (skips insert if already seen).
    Each catch doc also carries a 'date' string (YYYY-MM-DD UTC) for
    efficient per-day queries without scanning the full timestamp range.
    """
    if message_id is not None:
        if not await mark_catch_message_seen(message_id):
            return False  # duplicate — already recorded

    now      = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    await get_db().catches.insert_one({
        "guild_id":    guild_id,
        "user_id":     user_id,
        "pokemon":     pokemon,
        "iv":          iv,
        "shiny":       shiny,
        "gigantamax":  gigantamax,
        "chain_shiny": chain_shiny,
        "channel_id":  channel_id,
        "message_id":  message_id,
        "timestamp":   now,
        "date":        date_str,   # UTC date bucket for cheap per-day queries
    })
    return True


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
    """Returns catch stats for today (UTC date bucket)."""
    db       = get_db()
    date_str = today_label()
    pipeline = [
        {"$match": {"guild_id": guild_id, "user_id": user_id, "date": date_str}},
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
    db       = get_db()
    date_str = today_label()
    pipeline = [
        {"$match": {"guild_id": guild_id, "user_id": user_id, "date": date_str}},
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
    db       = get_db()
    date_str = today_label()
    since    = _today_start()
    plist    = list(category_pokemon)

    caught = await db.catches.count_documents({
        "guild_id": guild_id,
        "pokemon":  {"$in": plist},
        "date":     date_str,
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
    """Global catch leaderboard for today (UTC date bucket)."""
    db       = get_db()
    date_str = today_label()
    pipeline = [
        {"$match": {"guild_id": guild_id, "date": date_str}},
        {"$group": {
            "_id":        "$user_id",
            "total":      {"$sum": 1},
            "shiny":      {"$sum": {"$cond": [{"$and": ["$shiny", {"$not": ["$chain_shiny"]}]}, 1, 0]}},
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
            # exclude chain_shiny catches from plain shiny count to avoid double-counting
            "shiny":      {"$sum": {"$cond": [{"$and": ["$shiny", {"$not": ["$chain_shiny"]}]}, 1, 0]}},
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
    """Category catch leaderboard for today (UTC date bucket)."""
    db       = get_db()
    date_str = today_label()
    pipeline = [
        {"$match": {
            "guild_id": guild_id,
            "pokemon":  {"$in": list(category_pokemon)},
            "date":     date_str,
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
    """Shiny catch leaderboard for today (UTC date bucket)."""
    db       = get_db()
    date_str = today_label()
    pipeline = [
        {"$match": {
            "guild_id": guild_id,
            "date":     date_str,
            "$or": [{"shiny": True}, {"chain_shiny": True}],
        }},
        {"$group": {
            "_id":         "$user_id",
            "shiny":       {"$sum": {"$cond": [{"$and": ["$shiny", {"$not": ["$chain_shiny"]}]}, 1, 0]}},
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
            # chain_shiny catches may also have shiny=True; count them only in chain_shiny
            "shiny":       {"$sum": {"$cond": [{"$and": ["$shiny", {"$not": ["$chain_shiny"]}]}, 1, 0]}},
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
    """Gigantamax catch leaderboard for today (UTC date bucket)."""
    db       = get_db()
    date_str = today_label()
    pipeline = [
        {"$match": {
            "guild_id":   guild_id,
            "date":       date_str,
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
    [Owner only] Delete ALL catches, flees, and the catch dedup cache for a guild.
    catch_seen_messages has no guild_id so we wipe it entirely (pure dedup cache).
    Returns {"catches": int, "flees": int, "seen_messages": int} with deleted counts.
    """
    db = get_db()
    catch_result = await db.catches.delete_many({"guild_id": guild_id})
    flee_result  = await db.flees.delete_many({"guild_id": guild_id})
    seen_result  = await db.catch_seen_messages.delete_many({})
    return {
        "catches":       catch_result.deleted_count,
        "flees":         flee_result.deleted_count,
        "seen_messages": seen_result.deleted_count,
    }


# Add this to db.py — new collection helpers for custom pokemon lists

async def get_custom_pokemon_list(guild_id: int) -> list[str]:
    """Get the list of custom Pokémon that trigger autolocks for this guild."""
    doc = await get_db().custom_pokemon_lists.find_one({"guild_id": guild_id})
    return doc.get("pokemon_list", []) if doc else []


async def add_custom_pokemon(guild_id: int, pokemon: str) -> bool:
    """Add a Pokémon to the custom list. Returns True if added, False if already exists."""
    pokemon_lower = pokemon.lower()
    doc = await get_db().custom_pokemon_lists.find_one({"guild_id": guild_id})

    if doc and pokemon_lower in [p.lower() for p in doc.get("pokemon_list", [])]:
        return False  # Already in list

    await get_db().custom_pokemon_lists.update_one(
        {"guild_id": guild_id},
        {"$addToSet": {"pokemon_list": pokemon}},
        upsert=True,
    )
    return True


async def remove_custom_pokemon(guild_id: int, pokemon: str) -> bool:
    """Remove a Pokémon from the custom list. Returns True if removed, False if not found."""
    pokemon_lower = pokemon.lower()
    doc = await get_db().custom_pokemon_lists.find_one({"guild_id": guild_id})

    if not doc or pokemon_lower not in [p.lower() for p in doc.get("pokemon_list", [])]:
        return False  # Not in list

    await get_db().custom_pokemon_lists.update_one(
        {"guild_id": guild_id},
        {"$pull": {"pokemon_list": {"$regex": f"^{pokemon}$", "$options": "i"}}},
    )
    return True


async def clear_custom_pokemon_list(guild_id: int) -> None:
    """Clear the entire custom Pokémon list for a guild."""
    await get_db().custom_pokemon_lists.delete_one({"guild_id": guild_id})


# ── Element Quiz leaderboard ──────────────────────────────────────────────────

async def quiz_add_score(scope_key: str, user_id: int) -> int:
    """
    Increment quiz score for user_id in the given scope.
    scope_key is the guild_id (as str) or "dm_{user_id}".
    Returns the new total score.
    """
    db = get_db()
    doc = await db.quiz_scores.find_one_and_update(
        {"scope_key": scope_key, "user_id": user_id},
        {"$inc": {"score": 1}},
        upsert=True,
        return_document=True,  # motor uses pymongo's ReturnDocument.AFTER semantics with True
    )
    return doc["score"]


async def quiz_get_scores(scope_key: str, limit: int = 10) -> list[dict]:
    """
    Return top-N scores for a scope, sorted descending.
    Returns [{"user_id": int, "score": int}, ...]
    """
    db = get_db()
    cursor = (
        db.quiz_scores
        .find({"scope_key": scope_key}, {"_id": 0, "user_id": 1, "score": 1})
        .sort("score", -1)
        .limit(limit)
    )
    return await cursor.to_list(limit)


# ── Loans ─────────────────────────────────────────────────────────────────────

async def _next_loan_id(guild_id: int) -> str:
    """Generate sequential loan IDs per guild: L-00001, L-00002, …"""
    db  = get_db()
    seq = await db.loan_counters.find_one_and_update(
        {"guild_id": guild_id},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return f"L-{seq['seq']:05d}"


def _compute_amount_due(principal: int, rate: float, interest_type: str) -> float:
    """
    Calculate total amount due at issuance for flat/none interest.
    For compound interest, amount_due is recalculated at repayment time.
    """
    if interest_type == "none" or rate == 0:
        return float(principal)
    if interest_type == "flat":
        return round(principal * (1 + rate), 2)
    # compound: stored as principal; recalculated later with elapsed days
    return float(principal)


def compute_compound_due(principal: int, rate: float, created_at: datetime, as_of: datetime = None) -> float:
    """
    Daily compound interest: amount = principal × (1 + rate)^days
    Rate is daily (e.g. 0.01 = 1 % per day).
    Pass as_of to calculate as of a specific date (defaults to now UTC).
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    # make both tz-aware for subtraction
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    days = max(0, (as_of - created_at).total_seconds() / 86400)
    return round(principal * math.pow(1 + rate, days), 2)


async def create_loan(
    guild_id:      int,
    lender_id:     int,
    borrower_id:   int,
    principal:     int,
    currency:      str   = "pc",
    interest_rate: float = 0.0,
    interest_type: str   = "none",   # "none" | "flat" | "compound"
    due_date:      "datetime | None" = None,
    proof_url:     str   = None,
    note:          str   = None,
    created_at:    "datetime | None" = None,  # override grant date; defaults to now
) -> dict:
    """Insert a new loan and return the full document."""
    db      = get_db()
    loan_id = await _next_loan_id(guild_id)
    amount_due = _compute_amount_due(principal, interest_rate, interest_type)
    now = created_at if created_at is not None else datetime.now(timezone.utc)

    doc = {
        "loan_id":       loan_id,
        "guild_id":      guild_id,
        "lender_id":     lender_id,
        "borrower_id":   borrower_id,
        "currency":      currency.lower(),
        "principal":     principal,
        "interest_rate": interest_rate,
        "interest_type": interest_type,
        "amount_due":    amount_due,
        "amount_paid":   0.0,
        "status":        "active",
        "proof_url":     proof_url,
        "note":          note,
        "created_at":    now,
        "due_date":      due_date,
        "paid_at":       None,
        "payments":      [],
    }
    await db.loans.insert_one(doc)
    return doc


async def get_loan(loan_id: str) -> dict | None:
    """Fetch a single loan by its human-readable ID."""
    return await get_db().loans.find_one({"loan_id": loan_id})


async def get_loans_as_lender(guild_id: int, lender_id: int, status: str = None) -> list[dict]:
    """All loans where this user is the lender. Optional status filter."""
    filt = {"guild_id": guild_id, "lender_id": lender_id}
    if status:
        filt["status"] = status
    return await get_db().loans.find(filt).sort("created_at", -1).to_list(None)


async def get_loans_as_borrower(guild_id: int, borrower_id: int, status: str = None) -> list[dict]:
    """All loans where this user is the borrower. Optional status filter."""
    filt = {"guild_id": guild_id, "borrower_id": borrower_id}
    if status:
        filt["status"] = status
    return await get_db().loans.find(filt).sort("created_at", -1).to_list(None)


async def get_all_guild_loans(guild_id: int, status: str = None) -> list[dict]:
    """Every loan in the guild. Optional status filter."""
    filt = {"guild_id": guild_id}
    if status:
        filt["status"] = status
    return await get_db().loans.find(filt).sort("created_at", -1).to_list(None)


async def record_payment(
    loan_id:    str,
    amount:     float,
    note:       str = None,
) -> dict | None:
    """
    Record a partial or full repayment.
    Automatically flips status to 'partial' or 'paid'.
    Returns the updated loan doc, or None if not found.
    """
    db  = get_db()
    now = datetime.now(timezone.utc)

    loan = await get_loan(loan_id)
    if not loan:
        return None

    new_paid = round(loan["amount_paid"] + amount, 2)

    # Recalculate amount_due for compound loans
    amount_due = loan["amount_due"]
    if loan["interest_type"] == "compound" and loan["interest_rate"] > 0:
        amount_due = compute_compound_due(
            loan["principal"], loan["interest_rate"], loan["created_at"]
        )

    if new_paid >= amount_due:
        new_status = "paid"
        paid_at    = now
    else:
        new_status = "partial"
        paid_at    = None

    payment_entry = {"amount": amount, "timestamp": now, "note": note}

    updated = await db.loans.find_one_and_update(
        {"loan_id": loan_id},
        {
            "$set": {
                "amount_paid": new_paid,
                "amount_due":  amount_due,
                "status":      new_status,
                "paid_at":     paid_at,
            },
            "$push": {"payments": payment_entry},
        },
        return_document=True,
    )
    return updated


async def cancel_loan(loan_id: str) -> dict | None:
    """Mark a loan as cancelled. Returns updated doc."""
    return await get_db().loans.find_one_and_update(
        {"loan_id": loan_id},
        {"$set": {"status": "cancelled"}},
        return_document=True,
    )


async def update_loan_proof(loan_id: str, proof_url: str) -> dict | None:
    """Attach or replace the proof URL on a loan."""
    return await get_db().loans.find_one_and_update(
        {"loan_id": loan_id},
        {"$set": {"proof_url": proof_url}},
        return_document=True,
    )


async def update_loan_note(loan_id: str, note: str) -> dict | None:
    """Update the free-text note on a loan."""
    return await get_db().loans.find_one_and_update(
        {"loan_id": loan_id},
        {"$set": {"note": note}},
        return_document=True,
    )


# ── Box openings ──────────────────────────────────────────────────────────────
#
# Collection: box_openings
# One document per (guild_id, user_id, date).
# Multiple openings on the same day are merged via $inc / $push.
#
# Collection: box_seen_messages
# One document per message_id — used to prevent double-recording.
# { message_id: int }
#
# Schema (box_openings):
# {
#   guild_id:      int,
#   user_id:       int,
#   date:          str,   # "YYYY-MM-DD" UTC
#   boxes_opened:  int,
#   total_pokemon: int,
#   total_coins:   int,
#   total_shards:  int,
#   total_redeems: int,
#   shinies:       [{"name": str, "iv": float}, ...],
#   high_iv:       [{"name": str, "iv": float}, ...],
#   low_iv:        [{"name": str, "iv": float}, ...],
# }


async def is_box_message_seen(message_id: int) -> bool:
    """Return True if this message_id has already been recorded."""
    doc = await get_db().box_seen_messages.find_one({"message_id": message_id})
    return doc is not None


async def mark_box_message_seen(message_id: int) -> bool:
    """
    Insert message_id into the seen-set.
    Returns True if newly inserted, False if it was already present
    (duplicate — caller should skip recording).
    Uses insert_one with duplicate-key handling so it is race-safe.
    """
    from pymongo.errors import DuplicateKeyError
    try:
        await get_db().box_seen_messages.insert_one({"message_id": message_id})
        return True   # freshly inserted → safe to record
    except DuplicateKeyError:
        return False  # already seen → skip


async def record_box_opening(
    guild_id:      int,
    user_id:       int,
    boxes_opened:  int,
    total_pokemon: int,
    shinies:       list[dict],
    high_iv:       list[dict],
    low_iv:        list[dict],
    total_coins:   int,
    total_shards:  int,
    total_redeems: int = 0,
    date_override: "datetime.date | None" = None,
) -> None:
    """
    Upsert a box-opening session into the daily document for this user.
    Multiple openings on the same day are accumulated — counters are
    incremented and notable-pull lists are appended to.
    Pass date_override (a datetime.date) to record against a historical date.
    """
    from datetime import date as _date
    date_str = (date_override or _date.today()).isoformat()

    await get_db().box_openings.update_one(
        {"guild_id": guild_id, "user_id": user_id, "date": date_str},
        {
            "$inc": {
                "boxes_opened":  boxes_opened,
                "total_pokemon": total_pokemon,
                "total_coins":   total_coins,
                "total_shards":  total_shards,
                "total_redeems": total_redeems,
            },
            "$push": {
                "shinies": {"$each": shinies},
                "high_iv": {"$each": high_iv},
                "low_iv":  {"$each": low_iv},
            },
        },
        upsert=True,
    )


async def get_box_stats(guild_id: int, user_id: int) -> list[dict]:
    """
    Return all daily box-opening documents for a user, sorted oldest → newest.
    Each document has the shape described in the schema above.
    Missing list fields are defaulted to [] so callers don't need to guard.
    """
    cursor = get_db().box_openings.find(
        {"guild_id": guild_id, "user_id": user_id},
        sort=[("date", 1)],
    )
    docs = await cursor.to_list(length=None)
    for d in docs:
        d.setdefault("shinies",       [])
        d.setdefault("high_iv",       [])
        d.setdefault("low_iv",        [])
        d.setdefault("total_redeems", 0)
        d.pop("_id", None)
    return docs


async def clear_box_data(guild_id: int) -> dict:
    """
    Delete ALL box-opening records and seen-message dedup entries for a guild.
    Returns {"box_openings": int, "seen_messages": int} with deleted counts.
    """
    db = get_db()
    openings_result = await db.box_openings.delete_many({"guild_id": guild_id})
    # seen_messages has no guild_id — we can only wipe the whole collection
    # (safe because it is purely a dedup cache and can be rebuilt by backfill)
    seen_result = await db.box_seen_messages.delete_many({})
    return {
        "box_openings":  openings_result.deleted_count,
        "seen_messages": seen_result.deleted_count,
    }


async def get_loan_summary(guild_id: int, user_id: int) -> dict:
    """
    Returns a summary dict for a user:
      lent_active   : total principal currently lent out (active/partial)
      borrowed_active : total principal currently owed (active/partial)
      lent_total    : lifetime principal lent
      borrowed_total: lifetime principal borrowed
      loans_given   : count of loans given
      loans_received: count of loans received
    """
    db = get_db()

    active_statuses = {"$in": ["active", "partial"]}

    # Active lent — sum remaining balance (amount_due - amount_paid) so partial
    # repayments are reflected and paid/cancelled loans are excluded by the status filter.
    pipeline_lent_active = [
        {"$match": {"guild_id": guild_id, "lender_id": user_id, "status": active_statuses}},
        {"$group": {
            "_id":   None,
            "total": {"$sum": {"$subtract": ["$amount_due", "$amount_paid"]}},
            "count": {"$sum": 1},
        }},
    ]
    # Active borrowed
    pipeline_borrowed_active = [
        {"$match": {"guild_id": guild_id, "borrower_id": user_id, "status": active_statuses}},
        {"$group": {
            "_id":   None,
            "total": {"$sum": {"$subtract": ["$amount_due", "$amount_paid"]}},
            "count": {"$sum": 1},
        }},
    ]
    # All-time lent
    pipeline_lent_all = [
        {"$match": {"guild_id": guild_id, "lender_id": user_id}},
        {"$group": {"_id": None, "total": {"$sum": "$principal"}, "count": {"$sum": 1}}},
    ]
    # All-time borrowed
    pipeline_borrowed_all = [
        {"$match": {"guild_id": guild_id, "borrower_id": user_id}},
        {"$group": {"_id": None, "total": {"$sum": "$principal"}, "count": {"$sum": 1}}},
    ]

    def _extract(result):
        if result:
            return result[0]["total"], result[0]["count"]
        return 0, 0

    la, _  = _extract(await db.loans.aggregate(pipeline_lent_active).to_list(1))
    ba, _  = _extract(await db.loans.aggregate(pipeline_borrowed_active).to_list(1))
    lt, lc = _extract(await db.loans.aggregate(pipeline_lent_all).to_list(1))
    bt, bc = _extract(await db.loans.aggregate(pipeline_borrowed_all).to_list(1))

    return {
        "lent_active":      la,
        "borrowed_active":  ba,
        "lent_total":       lt,
        "borrowed_total":   bt,
        "loans_given":      lc,
        "loans_received":   bc,
    }
"""
db_additions.py
═══════════════
Paste these functions into the BOTTOM of your existing db.py.
Nothing in the existing file is changed or removed.

New functions added
───────────────────
  reset_user_data          — wipe one user's catches + box_openings for a guild
  get_server_stats         — aggregated totals across all users, today
  get_server_stats_alltime — aggregated totals across all users, all time
  get_box_leaderboard      — top users by boxes_opened, today
  get_box_leaderboard_alltime — top users by boxes_opened, all time
  purge_old_flees          — delete flee docs older than N days (background task)

TTL / cleanup note
──────────────────
  Fled data is purged automatically every UTC midnight via a discord.ext.tasks
  loop defined in tracker_cog.py.  No TTL index is used so no other collection
  is ever touched by the purge.
"""

# ── Per-user data reset ───────────────────────────────────────────────────────

async def reset_user_data(guild_id: int, user_id: int) -> dict:
    """
    Permanently delete ALL catch and box-opening records for one user in a guild.
    Returns {"catches": int, "box_openings": int} with deleted counts.

    Dedup caches (catch_seen_messages, box_seen_messages) are intentionally
    NOT cleared — they are purely idempotency guards and clearing them would
    risk double-recording old messages if they are later re-processed.
    """
    db = get_db()
    catch_result = await db.catches.delete_many(
        {"guild_id": guild_id, "user_id": user_id}
    )
    box_result = await db.box_openings.delete_many(
        {"guild_id": guild_id, "user_id": user_id}
    )
    return {
        "catches":      catch_result.deleted_count,
        "box_openings": box_result.deleted_count,
    }


# ── Server-wide aggregated stats ──────────────────────────────────────────────

async def get_server_stats(guild_id: int) -> dict:
    """
    Aggregate catch + box stats across ALL users for today (UTC date bucket).
    Returns a flat dict with every counter needed for the server stats embed.
    """
    db       = get_db()
    date_str = today_label()

    # ── Catches ───────────────────────────────────────────────────────────────
    catch_pipeline = [
        {"$match": {"guild_id": guild_id, "date": date_str}},
        {"$group": {
            "_id":         None,
            "total":       {"$sum": 1},
            "shiny":       {"$sum": {"$cond": ["$shiny",       1, 0]}},
            "gigantamax":  {"$sum": {"$cond": ["$gigantamax",  1, 0]}},
            "chain_shiny": {"$sum": {"$cond": ["$chain_shiny", 1, 0]}},
        }},
    ]
    catch_res = await db.catches.aggregate(catch_pipeline).to_list(1)
    c = catch_res[0] if catch_res else {}

    # ── Box openings ──────────────────────────────────────────────────────────
    box_pipeline = [
        {"$match": {"guild_id": guild_id, "date": date_str}},
        {"$group": {
            "_id":          None,
            "boxes_opened":  {"$sum": "$boxes_opened"},
            "total_pokemon": {"$sum": "$total_pokemon"},
            "total_coins":   {"$sum": "$total_coins"},
            "total_shards":  {"$sum": "$total_shards"},
            "total_redeems": {"$sum": "$total_redeems"},
            "total_shinies": {"$sum": {"$size": {"$ifNull": ["$shinies", []]}}},
        }},
    ]
    box_res = await db.box_openings.aggregate(box_pipeline).to_list(1)
    b = box_res[0] if box_res else {}

    return {
        # catches
        "catches":      c.get("total",       0),
        "shiny":        c.get("shiny",        0),
        "gigantamax":   c.get("gigantamax",   0),
        "chain_shiny":  c.get("chain_shiny",  0),
        # box
        "boxes_opened":  b.get("boxes_opened",  0),
        "total_pokemon": b.get("total_pokemon", 0),
        "total_coins":   b.get("total_coins",   0),
        "total_shards":  b.get("total_shards",  0),
        "total_redeems": b.get("total_redeems", 0),
        "box_shinies":   b.get("total_shinies", 0),
    }


async def get_server_stats_alltime(guild_id: int) -> dict:
    """
    Aggregate catch + box stats across ALL users — no time filter.
    Same shape as get_server_stats().
    """
    db = get_db()

    catch_pipeline = [
        {"$match": {"guild_id": guild_id}},
        {"$group": {
            "_id":         None,
            "total":       {"$sum": 1},
            "shiny":       {"$sum": {"$cond": ["$shiny",       1, 0]}},
            "gigantamax":  {"$sum": {"$cond": ["$gigantamax",  1, 0]}},
            "chain_shiny": {"$sum": {"$cond": ["$chain_shiny", 1, 0]}},
        }},
    ]
    catch_res = await db.catches.aggregate(catch_pipeline).to_list(1)
    c = catch_res[0] if catch_res else {}

    box_pipeline = [
        {"$match": {"guild_id": guild_id}},
        {"$group": {
            "_id":           None,
            "boxes_opened":  {"$sum": "$boxes_opened"},
            "total_pokemon": {"$sum": "$total_pokemon"},
            "total_coins":   {"$sum": "$total_coins"},
            "total_shards":  {"$sum": "$total_shards"},
            "total_redeems": {"$sum": "$total_redeems"},
            "total_shinies": {"$sum": {"$size": {"$ifNull": ["$shinies", []]}}},
        }},
    ]
    box_res = await db.box_openings.aggregate(box_pipeline).to_list(1)
    b = box_res[0] if box_res else {}

    return {
        "catches":      c.get("total",       0),
        "shiny":        c.get("shiny",        0),
        "gigantamax":   c.get("gigantamax",   0),
        "chain_shiny":  c.get("chain_shiny",  0),
        "boxes_opened":  b.get("boxes_opened",  0),
        "total_pokemon": b.get("total_pokemon", 0),
        "total_coins":   b.get("total_coins",   0),
        "total_shards":  b.get("total_shards",  0),
        "total_redeems": b.get("total_redeems", 0),
        "box_shinies":   b.get("total_shinies", 0),
    }


# ── Box leaderboard ───────────────────────────────────────────────────────────

async def get_box_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    """
    Top users ranked by boxes_opened today (UTC date bucket).
    Returns [{"user_id": int, "boxes_opened": int, "total_pokemon": int,
              "total_shinies": int}, ...]
    """
    db       = get_db()
    date_str = today_label()
    pipeline = [
        {"$match": {"guild_id": guild_id, "date": date_str}},
        {"$group": {
            "_id":           "$user_id",
            "boxes_opened":  {"$sum": "$boxes_opened"},
            "total_pokemon": {"$sum": "$total_pokemon"},
            "total_shinies": {"$sum": {"$size": {"$ifNull": ["$shinies", []]}}},
        }},
        {"$sort":  {"boxes_opened": -1}},
        {"$limit": limit},
    ]
    docs = await db.box_openings.aggregate(pipeline).to_list(limit)
    return [
        {
            "user_id":       d["_id"],
            "total":         d["boxes_opened"],   # "total" key keeps leaderboard renderer happy
            "boxes_opened":  d["boxes_opened"],
            "total_pokemon": d["total_pokemon"],
            "total_shinies": d["total_shinies"],
        }
        for d in docs
    ]


async def get_box_leaderboard_alltime(guild_id: int, limit: int = 10) -> list[dict]:
    """
    Top users ranked by boxes_opened across all time.
    Same shape as get_box_leaderboard().
    """
    db = get_db()
    pipeline = [
        {"$match": {"guild_id": guild_id}},
        {"$group": {
            "_id":           "$user_id",
            "boxes_opened":  {"$sum": "$boxes_opened"},
            "total_pokemon": {"$sum": "$total_pokemon"},
            "total_shinies": {"$sum": {"$size": {"$ifNull": ["$shinies", []]}}},
        }},
        {"$sort":  {"boxes_opened": -1}},
        {"$limit": limit},
    ]
    docs = await db.box_openings.aggregate(pipeline).to_list(limit)
    return [
        {
            "user_id":       d["_id"],
            "total":         d["boxes_opened"],
            "boxes_opened":  d["boxes_opened"],
            "total_pokemon": d["total_pokemon"],
            "total_shinies": d["total_shinies"],
        }
        for d in docs
    ]


# ── Fled data cleanup (called nightly by tracker_cog task) ───────────────────

async def purge_old_flees(days: int = 7) -> int:
    """
    Delete flee documents older than `days` days.
    Only the `flees` collection is touched — no other data is affected.
    Returns the number of documents deleted.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await get_db().flees.delete_many({"timestamp": {"$lt": cutoff}})
    return result.deleted_count

"""
db_loans_additions.py
═════════════════════
Paste the functions below into db.py.

Two existing functions are REPLACED (find them by name and swap them out):
  • record_payment   — now accepts proof_url + paid_date per payment
  • update_loan_proof — replaced by update_loan_proof_with_meta

Three new functions are ADDED:
  • update_loan_proof_with_meta
  • reset_all_loans
  • reset_user_loans
"""


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE the existing record_payment() with this version.
# New params: proof_url (str|None), paid_date (str|None, e.g. "2025-08-01")
# ─────────────────────────────────────────────────────────────────────────────

async def record_payment(
    loan_id:   str,
    amount:    float,
    note:      str = None,
    proof_url: str = None,
    paid_date: str = None,   # "YYYY-MM-DD" string supplied by user, optional
) -> dict | None:
    """
    Record a partial or full repayment.
    Automatically flips status to 'partial' or 'paid'.
    Stores proof_url and paid_date in the payment sub-document.
    Returns the updated loan doc, or None if not found.
    """
    db  = get_db()
    now = datetime.now(timezone.utc)

    loan = await get_loan(loan_id)
    if not loan:
        return None

    new_paid = round(loan["amount_paid"] + amount, 2)

    # Recalculate amount_due for compound loans
    amount_due = loan["amount_due"]
    if loan["interest_type"] == "compound" and loan["interest_rate"] > 0:
        amount_due = compute_compound_due(
            loan["principal"], loan["interest_rate"], loan["created_at"]
        )

    new_status = "paid" if new_paid >= amount_due else "partial"
    paid_at    = now if new_status == "paid" else None

    payment_entry = {
        "amount":    amount,
        "timestamp": now,
        "note":      note,
        "proof_url": proof_url,
        "paid_date": paid_date,
    }

    updated = await db.loans.find_one_and_update(
        {"loan_id": loan_id},
        {
            "$set": {
                "amount_paid": new_paid,
                "amount_due":  amount_due,
                "status":      new_status,
                "paid_at":     paid_at,
            },
            "$push": {"payments": payment_entry},
        },
        return_document=True,
    )
    return updated


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE the existing update_loan_proof() with this version.
# ─────────────────────────────────────────────────────────────────────────────

async def update_loan_proof_with_meta(
    loan_id:   str,
    proof_url: str,
    note:      str = None,
    paid_date: str = None,
) -> dict | None:
    """
    Attach or replace the top-level proof URL on a loan.
    Optionally also records a note and/or paid_date alongside it.
    Returns the updated loan doc.
    """
    fields = {"proof_url": proof_url}
    if note:
        fields["proof_note"] = note
    if paid_date:
        fields["proof_paid_date"] = paid_date

    return await get_db().loans.find_one_and_update(
        {"loan_id": loan_id},
        {"$set": fields},
        return_document=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# NEW — reset helpers (bot owner only)
# ─────────────────────────────────────────────────────────────────────────────

async def reset_all_loans(guild_id: int) -> int:
    """
    Permanently delete ALL loan documents for a guild.
    Also resets the loan ID counter so IDs restart from L-00001.
    Returns the number of loans deleted.
    """
    db = get_db()
    result = await db.loans.delete_many({"guild_id": guild_id})
    # Reset the sequential counter so IDs restart cleanly
    await db.loan_counters.delete_one({"guild_id": guild_id})
    return result.deleted_count


async def reset_user_loans(guild_id: int, user_id: int) -> int:
    """
    Permanently delete all loans where this user is either the lender or the
    borrower, within the given guild.
    Returns the number of loans deleted.
    """
    db = get_db()
    result = await db.loans.delete_many({
        "guild_id": guild_id,
        "$or": [
            {"lender_id":   user_id},
            {"borrower_id": user_id},
        ],
    })
    return result.deleted_count


# ─────────────────────────────────────────────────────────────────────────────
# NEW — battle_cog win/loss tracking
# One doc per human trainer per finished battle. Split by vs_ai so PvP
# results and "vs the bot" results can be reported separately (see !pf).
# ─────────────────────────────────────────────────────────────────────────────

async def record_battle_result(guild_id: int, user_id: int, vs_ai: bool, won: bool) -> None:
    """
    Record the outcome of one finished battle for one human trainer.
    Called once per human participant — a PvP battle logs one doc for each
    side, a `!battle @<bot>` fight logs a single doc for the human side.
    """
    await get_db().battle_results.insert_one({
        "guild_id":  guild_id,
        "user_id":   user_id,
        "vs_ai":     vs_ai,
        "won":       won,
        "timestamp": datetime.now(timezone.utc),
    })


async def get_battle_stats(guild_id: int, user_id: int) -> dict:
    """
    Returns a trainer's all-time battle record, split into PvP ("human")
    and vs-the-bot ("ai") buckets:
      {
        "human_total": int, "human_wins": int, "human_losses": int,
        "ai_total":    int, "ai_wins":    int, "ai_losses":    int,
      }
    """
    db = get_db()
    pipeline = [
        {"$match": {"guild_id": guild_id, "user_id": user_id}},
        {"$group": {
            "_id":   "$vs_ai",
            "total": {"$sum": 1},
            "wins":  {"$sum": {"$cond": ["$won", 1, 0]}},
        }},
    ]
    stats = {
        "human_total": 0, "human_wins": 0, "human_losses": 0,
        "ai_total":    0, "ai_wins":    0, "ai_losses":    0,
    }
    async for r in db.battle_results.aggregate(pipeline):
        total, wins = r["total"], r["wins"]
        prefix = "ai" if r["_id"] else "human"
        stats[f"{prefix}_total"]  = total
        stats[f"{prefix}_wins"]   = wins
        stats[f"{prefix}_losses"] = total - wins
    return stats


async def get_ai_global_stats(guild_id: int) -> dict:
    """
    The bot's own all-time record against every human trainer in a guild —
    the flip side of each individual trainer's "Vs AI" numbers from
    get_battle_stats(). Returns:
      {"total": int, "ai_wins": int, "ai_losses": int}
    where ai_wins/ai_losses are counted from the BOT's perspective (a
    battle_results doc's "won" field is from the human's perspective, so
    the bot won whenever the human's doc has won=False).
    """
    db = get_db()
    pipeline = [
        {"$match": {"guild_id": guild_id, "vs_ai": True}},
        {"$group": {
            "_id":        None,
            "total":      {"$sum": 1},
            "human_wins": {"$sum": {"$cond": ["$won", 1, 0]}},
        }},
    ]
    result = await db.battle_results.aggregate(pipeline).to_list(1)
    if not result:
        return {"total": 0, "ai_wins": 0, "ai_losses": 0}
    total = result[0]["total"]
    human_wins = result[0]["human_wins"]
    return {"total": total, "ai_wins": total - human_wins, "ai_losses": human_wins}
