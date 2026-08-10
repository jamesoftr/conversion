"""
cogs/battle/elo.py
────────────────────
Elo rating system for PvP battles, stored in its own `battle_elo`
collection, keyed by "<guild_id>:<user_id>", independent of the
win/loss stats in db .py.
"""

from .constants import _col

DEFAULT_ELO = 1000
ELO_K_WIN = 24        # base gain on a win
ELO_K_LOSS = 16       # base cost of a loss — smaller than the win gain, so
                       # winning nets more than losing costs on average
ELO_MIN_LOSS_PENALTY = 6   # a loss always costs at least this much
ELO_MAX_DELTA = 40         # ...but never swings more than this in one battle
ELO_FLOOR = DEFAULT_ELO    # rating can never drop below the 1000 starting point


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

