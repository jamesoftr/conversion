"""
cogs/battle/trainer_ai.py
────────────────────────────
Trainer/PendingChallenge dataclasses and the `!battle ai` opponent AI
(move scoring +  voluntary switching).
"""

from dataclasses import dataclass, field
from typing import Optional

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
SWITCH_INCOMING_THRESHOLD = 0.5  # ...or if the foe's predicted hit would take this much of OUR current HP
SWITCH_KILL_THRESHOLD = 0.45     # a candidate's best move must clear at least this much of the foe's max
                                  # HP to count as a real "can kill it back" threat - not just chip damage


def _accuracy_weighted_score(attacker: BattlePokemon, defender: BattlePokemon, move: dict) -> float:
    """Expected damage, discounted by the move's accuracy - a 150-power
    move that misses half the time should usually lose out to a reliable
    90-power move, not just whichever number is bigger on paper."""
    acc = move.get("accuracy")
    acc_frac = 1.0 if acc is None else acc / 100
    return estimate_damage(attacker, defender, move) * acc_frac


def _best_matchup_fraction(attacker: BattlePokemon, defender: BattlePokemon) -> float:
    """The attacker's single best accuracy-weighted move, as a fraction of
    the defender's max HP - used to judge whether a Pokemon (bot's current
    mon, or a bench candidate) can actually threaten a kill on the foe.
    Moves with no PP left are skipped, same as a real trainer couldn't
    select them."""
    best = 0.0
    for m in attacker.moves:
        if m.get("current_pp", 1) <= 0:
            continue
        score = _accuracy_weighted_score(attacker, defender, m)
        if score > best:
            best = score
    return best / max(defender.max_hp, 1)


def _worst_case_fraction(attacker: BattlePokemon, defender: BattlePokemon) -> float:
    """The heaviest hit `defender` could take from ANY of attacker's known
    moves (accuracy-weighted damage, not just the single best one),
    expressed as a fraction of defender's CURRENT hp. Since the bot can
    see the opponent's whole moveset, this is what the AI treats as "the
    move the opponent is expected to use" against a given target -
    whichever one hurts that target the most - so a switch-in candidate
    is judged against every option the foe actually has, not just its
    most obvious STAB move."""
    worst = 0.0
    for m in attacker.moves:
        if m.get("current_pp", 1) <= 0:
            continue
        score = _accuracy_weighted_score(attacker, defender, m)
        if score > worst:
            worst = score
    return worst / max(defender.hp, 1)


def _predicted_opponent_move(opponent_active: BattlePokemon, our_target: BattlePokemon) -> Optional[dict]:
    """The move `opponent_active` is expected to use against `our_target`
    next turn - its single highest accuracy-weighted damage option. Purely
    informational (used for flavoring/consistency with _worst_case_fraction,
    which already picks the same move when scoring the incoming threat)."""
    best_move, best_score = None, -1.0
    for m in opponent_active.moves:
        if m.get("current_pp", 1) <= 0:
            continue
        score = _accuracy_weighted_score(opponent_active, our_target, m)
        if score > best_score:
            best_score, best_move = score, m
    return best_move


def bot_choose_action(trainer: Trainer, opponent: Trainer) -> tuple:
    """Battle AI for the bot's own trainer.

    Attacking: scores every move in its active Pokemon's move pool by
    accuracy-weighted expected damage (STAB, type effectiveness, and
    effective stats all factored in via estimate_damage), skips any move
    with no PP left, and attacks with the best one - or Struggles (index
    -1) if every move is out of PP. If the best move would flat-out KO the
    foe's current active this turn, the bot always takes the kill instead
    of switching away from a winning position.

    Switching: since the bot can see the opponent's entire moveset, it
    doesn't just compare "my best move vs their best move" - it works out
    the single heaviest hit the foe could land with ANY of its known moves
    (_worst_case_fraction) against both the Pokemon currently in and every
    bench candidate. A bench mon is only considered a real switch-in if:

      • it can survive that worst-case hit AND has a move strong enough to
        threaten a kill back (>= SWITCH_KILL_THRESHOLD of the foe's max
        HP) - "resists it" alone isn't enough, it has to be able to punish
        the foe too; or
      • it's faster than the foe's active AND still clears that same kill
        threshold - being faster only matters if it can also do something
        with the extra turn, not just stall.

    Among eligible candidates the AI picks whichever trades best (highest
    offense minus the risk of the hit it'd still take back), so a slower
    Pokemon that would just eat a huge hit and do nothing back is never
    sent in, even if it's the "type-neutral" pick. This is on top of the
    forced switches on faint handled by Battle.get_forced_switch."""
    active = trainer.active
    defender = opponent.active
    moves = active.moves

    if active.must_recharge:
        return ("recharge", None)

    available = [i for i, m in enumerate(moves) if m.get("current_pp", 1) > 0]
    if not available:
        best_idx, best_frac = -1, 0.0
    else:
        scores = {i: _accuracy_weighted_score(active, defender, moves[i]) for i in available}
        best_idx = max(available, key=lambda i: scores[i])
        best_frac = scores[best_idx] / max(defender.max_hp, 1)

        # A guaranteed (or near-guaranteed) kill this turn is always worth
        # taking - never switch away from a foe that's already going down.
        if estimate_damage(active, defender, moves[best_idx]) >= defender.hp:
            return ("move", best_idx)

    bench = [(i, p) for i, p in enumerate(trainer.team) if not p.fainted and p is not active]
    if not bench:
        return ("move", best_idx)

    # How hard the foe's best available option would hit US if we stay in
    # (fraction of our current HP) - this, not just "is my best move weak",
    # is what actually makes a matchup dangerous enough to consider bailing.
    incoming_frac = _worst_case_fraction(defender, active)
    matchup_is_bad = best_frac < SWITCH_HP_THRESHOLD or incoming_frac >= SWITCH_INCOMING_THRESHOLD
    if not matchup_is_bad:
        return ("move", best_idx)

    def_speed = defender.effective_stat("spe")
    best_switch_idx, best_switch_score = None, 0.0
    for i, candidate in bench:
        # Damage the candidate could take from the foe's single hardest
        # known move, as a fraction of the candidate's CURRENT hp (a
        # bench mon that already took damage earlier is judged on what it
        # actually has left, not a fresh max-HP assumption).
        cand_incoming = _worst_case_fraction(defender, candidate)
        cand_best_frac = _best_matchup_fraction(candidate, defender)
        cand_faster = (candidate.effective_stat("spe") > def_speed or
                       (candidate.effective_stat("spe") == def_speed
                        and candidate.base_speed > defender.base_speed))
        survives_worst_hit = cand_incoming < 1.0
        can_kill_back = cand_best_frac >= SWITCH_KILL_THRESHOLD

        eligible = can_kill_back and (survives_worst_hit or cand_faster)
        if not eligible:
            continue

        # Reward real offensive threat, penalize how hard it'd still get
        # hit back, and give a small bonus to speed since a faster mon
        # gets to land its hit before the foe can retaliate at all.
        score = cand_best_frac - cand_incoming * 0.5 + (0.1 if cand_faster else 0.0)
        if score > best_switch_score:
            best_switch_score = score
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
