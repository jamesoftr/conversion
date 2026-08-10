"""
cogs/battle/trainer_ai.py
────────────────────────────
Trainer/PendingChallenge dataclasses and the `!battle ai` opponent AI
(move scoring + voluntary switching).
"""

from dataclasses import dataclass, field

import discord

from .constants import LEVEL
from .engine import BattlePokemon, type_multiplier_for


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

