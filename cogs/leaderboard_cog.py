"""
cogs/leaderboard_cog.py  —  Leaderboard commands.

Features:
  • Dropdown to switch between "Last 24 Hours" and "All Time" views
  • Dynamic Discord timestamp showing exact reset moment
  • Clean embed layout with rank emojis and reply arrow

Commands:
  a!leaderboard [category]
"""

import time
import discord
from discord.ext import commands

import db
from categories import get_category, all_keys
from config import E


def _reset_unix(resets_in_h: float) -> int:
    """Convert hours-from-now into a Unix timestamp for Discord's <t:N:F> format."""
    return int(time.time() + resets_in_h * 3600)


def _build_lines(entries: list[dict], include_extras: bool = True) -> list[str]:
    lines = []
    for i, e in enumerate(entries, 1):
        rank  = E.rank_emoji(i)
        name  = e["display_name"]
        total = e["total"]

        extras = []
        if include_extras:
            if e.get("shiny"):      extras.append(f"{E.shiny} {e['shiny']}")
            if e.get("gigantamax"): extras.append(f"{E.gigantamax} {e['gigantamax']}")

        extra_str = f"  {' '.join(extras)}" if extras else ""
        lines.append(f"{E.reply} {rank} **{name}** — **{total}** caught{extra_str}")
    return lines


async def _resolve_entries(bot, guild, raw_entries, include_extras=True) -> list[dict]:
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


class LeaderboardView(discord.ui.View):

    def __init__(
        self,
        bot,
        guild:       discord.Guild,
        guild_id:    int,
        category:    dict | None,
        reset_unix:  int,
        resets_in_h: float,
    ):
        super().__init__(timeout=300)
        self.bot         = bot
        self.guild       = guild
        self.guild_id    = guild_id
        self.category    = category
        self.reset_unix  = reset_unix
        self.resets_in_h = resets_in_h
        self._current    = "24h"

    @discord.ui.select(
        placeholder="Switch view…",
        options=[
            discord.SelectOption(label="Last 24 Hours", value="24h",     emoji="📅", default=True),
            discord.SelectOption(label="All Time",      value="alltime", emoji="🏅"),
        ],
    )
    async def view_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        chosen = select.values[0]
        if chosen == self._current:
            await interaction.followup.send("Already showing that view.", ephemeral=True)
            return
        self._current = chosen
        for opt in select.options:
            opt.default = (opt.value == chosen)
        embed = await self._build_embed(chosen)
        await interaction.message.edit(embed=embed, view=self)

    async def _build_embed(self, mode: str) -> discord.Embed:
        is_24h = mode == "24h"

        if self.category:
            cat_name = self.category["name"]
            raw = (
                await db.get_category_leaderboard(self.guild_id, self.category["pokemon"])
                if is_24h else
                await db.get_category_leaderboard_alltime(self.guild_id, self.category["pokemon"])
            )
            include_extras = False
            base_title = f"{E.leaderboard} {cat_name} Leaderboard"
        else:
            raw = (
                await db.get_leaderboard(self.guild_id)
                if is_24h else
                await db.get_leaderboard_alltime(self.guild_id)
            )
            include_extras = True
            base_title = f"{E.leaderboard} Global Leaderboard"

        entries = await _resolve_entries(self.bot, self.guild, raw, include_extras)
        lines   = _build_lines(entries, include_extras)

        if is_24h:
            header = (
                f"> 📅 **Last 24 Hours**\n"
                f"> Window resets <t:{self.reset_unix}:R> — <t:{self.reset_unix}:F>"
            )
        else:
            header = "> 🏅 **All Time** — complete catch history"

        body = "\n".join(lines) if lines else "*No catches recorded yet.*"
        description = f"{header}\n\u200b\n{body}"

        return discord.Embed(
            title=base_title,
            description=description,
            color=discord.Color.gold(),
        )


class LeaderboardCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="leaderboard", aliases=["lb"])
    async def leaderboard(self, ctx: commands.Context, category: str = None):
        """
        Show the catch leaderboard. Use the dropdown to switch 24h ↔ all-time.

        Usage:
          a!leaderboard              — global leaderboard
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

        view  = LeaderboardView(self.bot, ctx.guild, guild_id, cat, reset_unix, reset_info["resets_in_h"])
        embed = await view._build_embed("24h")
        await ctx.reply(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
