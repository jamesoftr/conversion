"""
cogs/battle/runner.py
────────────────────────
The Battle class — turn loop, damage resolution, embeds, and end-of-battle
bookkeeping (win/loss stats, Elo, rematch prompt).
"""

import asyncio
import random
from typing import Optional, TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from ..battle_cog import BattleCog

from .constants import (
    LEVEL, AFK_FORFEIT_STRIKES, FALLBACK_MOVE,
    ABILITY_IMMUNITY, ABILITY_ABSORB_HEAL, SELF_KO_MOVES,
    STRUGGLE_RECOIL_FRACTION, item_label,
)
from .engine import (
    BattlePokemon, calc_damage, _apply_secondary_effects, _apply_status_ailment,
    _status_precheck, _apply_drain_recoil,
)
from .render import pick_background, render_battle_scene
from .trainer_ai import Trainer
from .ui import BattlePanel, ForcedSwitchView, RematchView
from .elo import apply_elo_result

import db as _db


def _item_display(p: BattlePokemon) -> str:
    """Item label for embeds — one-shot berries (Oran/Sitrus) show
    struck-through once they've been consumed, instead of just vanishing,
    so it's visible at a glance that the item already did its job."""
    label = item_label(p.item)
    return f"~~{label}~~" if p.item_used else label


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
                    item_suffix = f" 🎒 {_item_display(p)}" if p.item else ""
                    mon_lines.append(f"{marker} {p.name.title()} — {p.hp}/{p.max_hp} HP{item_suffix}")
                embed.add_field(
                    name=t.user.display_name,
                    value="\n".join(mon_lines)[:1024],
                    inline=True,
                )
            else:
                p = t.active
                item_line = f"🎒 {_item_display(p)}\n" if p.item else ""
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
 
