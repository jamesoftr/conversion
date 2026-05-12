"""
cogs/category_cog.py  —  Per-category stats.

Shows BOTH last-24-hour and all-time totals, plus a reset countdown.

Commands:
  a!catstat <category>   — Spawned / caught / fled stats
"""

import discord
from discord.ext import commands

import db
from categories import get_category, all_keys


class CategoryCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="catstat", aliases=["categorystat", "cs"])
    async def catstat(self, ctx: commands.Context, category: str = None):
        """
        Show spawn / catch / flee statistics for a category.
        Displays last-24-hour numbers alongside all-time totals.

        Usage:
          a!catstat rares
          a!catstat regionals
        """
        if not category:
            await ctx.reply(
                f"Usage: `a!catstat <category>`\n"
                f"Available: `{'`, `'.join(all_keys())}`"
            )
            return

        cat = get_category(category)
        if not cat:
            await ctx.reply(
                f"❌ Unknown category `{category}`.\n"
                f"Available: `{'`, `'.join(all_keys())}`"
            )
            return

        guild_id = ctx.guild.id

        stats_24h, stats_all, reset_info = (
            await db.get_category_stats(guild_id, cat["pokemon"]),
            await db.get_category_stats_alltime(guild_id, cat["pokemon"]),
            await db.get_window_reset_info(guild_id),
        )

        def catch_rate(caught, spawned):
            return f"{caught / spawned * 100:.1f}%" if spawned else "N/A"

        e = discord.Embed(
            title=f"📊 {cat['name']}",
            color=discord.Color.blue(),
        )

        # Last 24 h
        e.add_field(
            name="📅 Last 24 Hours",
            value=(
                f"Spawned: **{stats_24h['total_spawned']}**\n"
                f"✅ Caught: **{stats_24h['caught']}**\n"
                f"💨 Fled: **{stats_24h['fled']}**\n"
                f"Catch rate: **{catch_rate(stats_24h['caught'], stats_24h['total_spawned'])}**"
            ),
            inline=True,
        )

        # All time
        e.add_field(
            name="🏅 All Time",
            value=(
                f"Spawned: **{stats_all['total_spawned']}**\n"
                f"✅ Caught: **{stats_all['caught']}**\n"
                f"💨 Fled: **{stats_all['fled']}**\n"
                f"Catch rate: **{catch_rate(stats_all['caught'], stats_all['total_spawned'])}**"
            ),
            inline=True,
        )

        reset_str = db.fmt_reset(reset_info["resets_in_h"])
        e.set_footer(text=f"24-hour window · {reset_str}")

        await ctx.reply(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(CategoryCog(bot))
