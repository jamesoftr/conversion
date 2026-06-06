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
    await db.flees.create_index(  [("guild_id", 1), ("timestamp", -1)])

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
            # exclude chain_shiny catches from plain shiny count to avoid double-counting
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
) -> dict:
    """Insert a new loan and return the full document."""
    db      = get_db()
    loan_id = await _next_loan_id(guild_id)
    amount_due = _compute_amount_due(principal, interest_rate, interest_type)
    now = datetime.now(timezone.utc)

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

    # Active lent
    pipeline_lent_active = [
        {"$match": {"guild_id": guild_id, "lender_id": user_id, "status": active_statuses}},
        {"$group": {"_id": None, "total": {"$sum": "$principal"}, "count": {"$sum": 1}}},
    ]
    # Active borrowed
    pipeline_borrowed_active = [
        {"$match": {"guild_id": guild_id, "borrower_id": user_id, "status": active_statuses}},
        {"$group": {"_id": None, "total": {"$sum": "$principal"}, "count": {"$sum": 1}}},
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
