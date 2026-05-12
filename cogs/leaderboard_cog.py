"""
cogs/leaderboard_cog.py  —  Leaderboard commands.

Features:
  • Board-type dropdown: Catches / Shiny / Gigantamax / (Category)
  • Time-window dropdown: Last 24 Hours / All Time
  • Dynamic Discord timestamp for window reset
  • Pings the user who ran the command (allowed_mentions safe)

Commands:
  a!leaderboard [category]
  a!lb          [category]
"""

import time
import discord
from discord.ext import commands

import db
from categories import get_category, all_keys
from config import E


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_unix(resets_in_h: float) -> int:
    return int(time.time() + resets_in_h * 3600)


async def _resolve_entries(bot, guild, raw_entries) -> list[dict]:
    """Attach display_name to each raw entry dict."""
    out = []
    for e in raw_entries:
        member = guild.get_member(e["user_id"])
        if member:
            display = member.display_name
        else:
            try:
                user    = await bot.fetch_user(e["user_id"])
                display = user.display_name
            except Exception:
                display = f"<@{e['user_id']}>"
        out.append({**e, "display_name": display})
    return out


# Board-type constants
BOARD_CATCHES    = "catches"
BOARD_SHINY      = "shiny"
BOARD_GIGANTAMAX = "gigantamax"
BOARD_CATEGORY   = "category"


def _build_lines(entries: list[dict], board: str) -> list[str]:
    lines = []
    for i, e in enumerate(entries, 1):
        rank  = E.rank_emoji(i)
        name  = e["display_name"]
        total = e["total"]

        if board == BOARD_CATCHES:
            extras = []
            if e.get("shiny"):      extras.append(f"{E.shiny} {e['shiny']}")
            if e.get("gigantamax"): extras.append(f"{E.gigantamax} {e['gigantamax']}")
            suffix = f"  {'  '.join(extras)}" if extras else ""
            lines.append(f"{E.reply} {rank} **{name}** — **{total}** caught{suffix}")

        elif board == BOARD_SHINY:
            chain = e.get("chain_shiny", 0)
            parts = [f"{E.shiny} **{e.get('shiny', 0)}**"]
            if chain:
                parts.append(f"{E.chain_shiny} **{chain}**")
            lines.append(f"{E.reply} {rank} **{name}** — {' + '.join(parts)} shiny  *(total: {total})*")

        elif board == BOARD_GIGANTAMAX:
            lines.append(f"{E.reply} {rank} **{name}** — {E.gigantamax} **{total}**")

        else:  # category
            lines.append(f"{E.reply} {rank} **{name}** — **{total}** caught")

    return lines


# ── View ──────────────────────────────────────────────────────────────────────

class LeaderboardView(discord.ui.View):

    def __init__(
        self,
        bot,
        guild:       discord.Guild,
        guild_id:    int,
        invoker_id:  int,
        category:    dict | None,
        reset_unix:  int,
        resets_in_h: float,
    ):
        super().__init__(timeout=300)
        self.bot         = bot
        self.guild       = guild
        self.guild_id    = guild_id
        self.invoker_id  = invoker_id
        self.category    = category
        self.reset_unix  = reset_unix
        self.resets_in_h = resets_in_h

        # State
        self._board  = BOARD_CATEGORY if category else BOARD_CATCHES
        self._window = "24h"

        # Build selects dynamically so we can hide the board-type select
        # when a category is active (only catch data exists per-category)
        if not category:
            self._add_board_select()
        self._add_window_select()

    def _add_board_select(self):
        select = discord.ui.Select(
            placeholder="Board type…",
            row=0,
            options=[
                discord.SelectOption(
                    label="Catches",    value=BOARD_CATCHES,
                    emoji="📋",         default=True,
                ),
                discord.SelectOption(
                    label="Shiny",      value=BOARD_SHINY,
                    emoji=E.shiny,
                ),
                discord.SelectOption(
                    label="Gigantamax", value=BOARD_GIGANTAMAX,
                    emoji=E.gigantamax,
                ),
            ],
        )
        select.callback = self._board_callback
        self._board_select = select
        self.add_item(select)

    def _add_window_select(self):
        select = discord.ui.Select(
            placeholder="Time window…",
            row=1,
            options=[
                discord.SelectOption(
                    label="Last 24 Hours", value="24h",
                    emoji="📅",            default=True,
                ),
                discord.SelectOption(
                    label="All Time",      value="alltime",
                    emoji="🏅",
                ),
            ],
        )
        select.callback = self._window_callback
        self._window_select = select
        self.add_item(select)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _board_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        chosen = interaction.data["values"][0]
        if chosen == self._board:
            await interaction.followup.send("Already showing that board.", ephemeral=True)
            return
        self._board = chosen
        for opt in self._board_select.options:
            opt.default = (opt.value == chosen)
        await interaction.message.edit(embed=await self._build_embed(), view=self)

    async def _window_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        chosen = interaction.data["values"][0]
        if chosen == self._window:
            await interaction.followup.send("Already showing that window.", ephemeral=True)
            return
        self._window = chosen
        for opt in self._window_select.options:
            opt.default = (opt.value == chosen)
        await interaction.message.edit(embed=await self._build_embed(), view=self)

    # ── Embed builder ─────────────────────────────────────────────────────────

    async def _build_embed(self) -> discord.Embed:
        is_24h  = self._window == "24h"
        gid     = self.guild_id
        board   = self._board

        # ── Fetch data ────────────────────────────────────────────────────────
        if board == BOARD_CATEGORY and self.category:
            raw = (
                await db.get_category_leaderboard(gid, self.category["pokemon"])
                if is_24h else
                await db.get_category_leaderboard_alltime(gid, self.category["pokemon"])
            )
            title = f"{E.leaderboard} {self.category['name']} Leaderboard"

        elif board == BOARD_SHINY:
            raw = (
                await db.get_shiny_leaderboard(gid)
                if is_24h else
                await db.get_shiny_leaderboard_alltime(gid)
            )
            title = f"{E.leaderboard} Shiny Leaderboard"

        elif board == BOARD_GIGANTAMAX:
            raw = (
                await db.get_gigantamax_leaderboard(gid)
                if is_24h else
                await db.get_gigantamax_leaderboard_alltime(gid)
            )
            title = f"{E.leaderboard} {E.gigantamax} Gigantamax Leaderboard"

        else:  # BOARD_CATCHES (global)
            raw = (
                await db.get_leaderboard(gid)
                if is_24h else
                await db.get_leaderboard_alltime(gid)
            )
            title = f"{E.leaderboard} Global Leaderboard"

        entries = await _resolve_entries(self.bot, self.guild, raw)
        lines   = _build_lines(entries, board)

        # ── Window header ─────────────────────────────────────────────────────
        if is_24h:
            window_header = (
                f"> 📅 **Last 24 Hours**\n"
                f"> Resets <t:{self.reset_unix}:R> — <t:{self.reset_unix}:F>"
            )
        else:
            window_header = "> 🏅 **All Time** — complete history"

        body        = "\n".join(lines) if lines else "*No data recorded yet.*"
        description = f"{window_header}\n\u200b\n{body}"

        return discord.Embed(
            title=title,
            description=description,
            color=discord.Color.gold(),
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class LeaderboardCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="leaderboard", aliases=["lb"])
    async def leaderboard(self, ctx: commands.Context, category: str = None):
        """
        Show the leaderboard with board-type and time-window dropdowns.

        Usage:
          a!leaderboard              — global (catches / shiny / gigantamax)
          a!leaderboard rares        — rares category leaderboard
        """
        guild_id = ctx.guild.id

        cat = None
        if category:
            cat = get_category(category)
            if not cat:
                await ctx.reply(
                    f"❌ Unknown category `{category}`.\n"
                    f"Available: `{'`, `'.join(all_keys())}`"
                )
                return

        reset_info = await db.get_window_reset_info(guild_id)
        reset_unix = _reset_unix(reset_info["resets_in_h"])

        view  = LeaderboardView(
            bot         = self.bot,
            guild       = ctx.guild,
            guild_id    = guild_id,
            invoker_id  = ctx.author.id,
            category    = cat,
            reset_unix  = reset_unix,
            resets_in_h = reset_info["resets_in_h"],
        )
        embed = await view._build_embed()

        await ctx.reply(embed=embed, view=view, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
