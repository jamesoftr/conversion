"""
cogs/category_cog.py  —  Per-category stats.
Shows today's and all-time stats in a clean embed layout
with a dynamic Discord timestamp for the window reset (UTC midnight).

Commands:
  a!catstat <category>
"""

import discord
from discord.ext import commands

import db
from categories import get_category, all_keys
from config import E


def _catch_rate(caught: int, spawned: int) -> str:
    return f"{caught / spawned * 100:.1f}%" if spawned else "—"


class CategoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="catstat", aliases=["categorystat", "cs"])
    async def catstat(self, ctx: commands.Context, category: str = None):
        """
        Show spawn / catch / flee statistics for a category.

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
        s24, sal, reset_info = (
            await db.get_category_stats(guild_id, cat["pokemon"]),
            await db.get_category_stats_alltime(guild_id, cat["pokemon"]),
            await db.get_window_reset_info(guild_id),
        )

        reset_unix  = reset_info["reset_unix"]
        today_label = reset_info["today_label"]

        rows = [
            ("🌿 Spawned", s24["total_spawned"], sal["total_spawned"]),
            ("✅ Caught",  s24["caught"],         sal["caught"]),
            ("💨 Fled",    s24["fled"],           sal["fled"]),
            ("📈 Rate",
             _catch_rate(s24["caught"], s24["total_spawned"]),
             _catch_rate(sal["caught"], sal["total_spawned"])),
        ]

        W_LABEL = 12
        W_COL   = 10
        header  = f"{'':>{W_LABEL}}  {'Today':^{W_COL}}  {'All Time':^{W_COL}}"
        divider = f"{'─' * W_LABEL}  {'─' * W_COL}  {'─' * W_COL}"
        data_lines = [
            f"{label:<{W_LABEL}}  {str(v24):^{W_COL}}  {str(vall):^{W_COL}}"
            for label, v24, vall in rows
        ]

        table = "\n".join([header, divider] + data_lines)
        reset_line = (
            f"> 📅 **Today — {today_label}**\n"
            f"> Resets <t:{reset_unix}:R> — <t:{reset_unix}:t>"
        )
        description = f"```\n{table}\n```\n{reset_line}"

        embed = discord.Embed(
            title=f"{E.category} {cat['name']}",
            description=description,
            color=discord.Color.blue(),
        )
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CategoryCog(bot))
