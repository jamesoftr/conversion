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
    Shows a trainer's Elo battle rating (starts at 1000, floored at 1000 —
    it can never drop below the starting rating). A win nets more rating
    than a loss costs, and losses cost more the higher your own rating
    climbs. Human-only: battles against the bot aren't rated, since the
    bot doesn't hold an Elo of its own.

!elo lb
    Shows the server's Elo leaderboard (top 10), humans only.

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
    matchup, not just when forced by a faint. Since it can see the foe's
    whole moveset, it checks the heaviest hit ANY of their moves could
    land (not just their obvious STAB pick) before bailing, and only sends
    in a bench mon that can either survive that hit and threaten a real
    kill back, or is fast enough to hit first and still do the same —
    "resists it but can't do anything back" is never good enough on its
    own. See cogs/battle/trainer_ai.py's bot_choose_action().
  • AI Gyms (`cogs/gym_cog.py`, `!gym <type>`) — 18 type-themed gym
    leaders, one per type. The trainer submits their team first (hidden
    from the AI), then the gym builds a team of that type specifically
    countering it (typing + BST) with movesets biased toward hitting that
    team hard, and battles it out with the same AI above. A win awards
    that gym's badge (`!bpf` shows earned badges).

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
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from .battle.constants import LEVEL, GYM_TYPES, GYM_TYPE_EMOJI, GYM_TOTAL_BADGES
from .battle.pokeapi import (
    get_pokemon_data, pick_moves, parse_bst_filter, format_bst_filter, build_team,
)
from .battle.engine import BattlePokemon
from .battle.trainer_ai import Trainer, PendingChallenge
from .battle.ui import ChallengeView
from .battle.runner import Battle
from .battle.elo import _get_elo

import db as _db


def _col():
    return _db.get_db()


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
            # Both teams' Pokemon are rolled concurrently (instead of one
            # at a time) — see cogs/battle/pokeapi.py's module docstring
            # for why this is what actually fixes the multi-minute waits.
            t1.team, t2.team = await asyncio.gather(
                build_team(self.session, count, min_total, max_total),
                build_team(self.session, count, min_total, max_total),
            )
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
            # Both teams' Pokemon are rolled concurrently (instead of one
            # at a time) — see cogs/battle/pokeapi.py's module docstring
            # for why this is what actually fixes the multi-minute waits.
            t1.team, t2.team = await asyncio.gather(
                build_team(self.session, count, min_total, max_total),
                build_team(self.session, count, min_total, max_total),
            )
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
            # Both teams' Pokemon are rolled concurrently (instead of one
            # at a time) — see cogs/battle/pokeapi.py's module docstring
            # for why this is what actually fixes the multi-minute waits.
            t1.team, t2.team = await asyncio.gather(
                build_team(self.session, count, min_total, max_total),
                build_team(self.session, count, min_total, max_total),
            )
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
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
            wr = f" ({stats['ai_wins'] / stats['total']:.0%})" if stats["total"] else ""
            embed.add_field(
                name="Vs All Trainers",
                value=(f"Total battles: **{stats['total']}**\n"
                       f"Win: **{stats['ai_wins']}**\n"
                       f"Loss: **{stats['ai_losses']}**{wr}"),
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
        badges = await _db.get_gym_badges(guild_id, member.id)
        badge_set = set(badges)

        def _wl(total, wins, losses):
            wr = f" ({wins / total:.0%})" if total else ""
            return f"Battles: **{total}**\nW/L: **{wins}**/**{losses}**{wr}"

        embed = discord.Embed(
            title=f"⚔️ {member.display_name}'s Trainer Profile",
            colour=0x3498DB,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="🧑‍🤝‍🧑 Vs Humans", value=_wl(
            stats["human_total"], stats["human_wins"], stats["human_losses"]
        ), inline=True)
        embed.add_field(name="🤖 Vs AI", value=_wl(
            stats["ai_total"], stats["ai_wins"], stats["ai_losses"]
        ), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer to keep the 3rd column empty/aligned

        badge_line = " ".join(
            GYM_TYPE_EMOJI.get(t, "🏅") for t in GYM_TYPES if t in badge_set
        ) or "*None yet — try `!gym` to challenge one!*"
        embed.add_field(
            name=f"🏅 Gym Badges ({len(badges)}/{GYM_TOTAL_BADGES})",
            value=badge_line,
            inline=False,
        )
        embed.set_footer(text=f"{member.display_name} • Level {LEVEL} trainer")
        await ctx.send(embed=embed)

    @commands.group(name="elo", invoke_without_command=True)
    async def elo(self, ctx: commands.Context, *, target: Optional[str] = None):
        """!elo [@user] — shows a trainer's Elo battle rating (starts at
        1000, floored at 1000). Elo is human-only — the bot doesn't hold
        a rating since `!battle @<bot>` fights aren't scored."""
        guild_id = ctx.guild.id if ctx.guild else 0

        member = ctx.author
        if target and target.strip().lower() not in ("", ):
            if target.strip().lower() in ("ai", "bot", self.bot.user.name.lower()):
                await ctx.send("I don't have an Elo rating — only human trainers are rated.")
                return
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

    @elo.command(name="lb")
    async def elo_leaderboard(self, ctx: commands.Context):
        """!elo lb — shows the server's Elo leaderboard (top 10). Humans
        only; the bot doesn't hold a rating."""
        guild_id = ctx.guild.id if ctx.guild else 0
        cursor = _col().battle_elo.find(
            {"guild_id": guild_id, "user_id": {"$ne": self.bot.user.id}}
        ).sort("elo", -1).limit(10)
        docs = [doc async for doc in cursor]

        if not docs:
            await ctx.send("No rated battles have been played in this server yet.")
            return

        lines = []
        for i, doc in enumerate(docs, start=1):
            user_id = doc["user_id"]
            member = ctx.guild.get_member(user_id) if ctx.guild else None
            name = member.display_name if member else f"<@{user_id}>"
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
