"""
cogs/category_cog.py  —  Per-category stats.

Shows last-24-hour and all-time stats in a clean embed layout
with a dynamic Discord timestamp for the window reset.

Commands:
  a!catstat <category>
"""

import time
import discord
from discord.ext import commands

import db
from categories import get_category, all_keys
from config import E


def _reset_unix(resets_in_h: float) -> int:
    return int(time.time() + resets_in_h * 3600)


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

        ru = _reset_unix(reset_info["resets_in_h"])

        # ── Description: compact stat grid ───────────────────────────────────
        #
        #   > 📊 Rares
        #   >
        #   >            Last 24h     All Time
        #   >  🌿 Spawned   12           340
        #   >  ✅ Caught      9           271
        #   >  💨 Fled        3            69
        #   >  📈 Rate     75.0%        79.7%
        #
        # Using a code-block column layout inside a blockquote for clean alignment.

        rows = [
            ("🌿 Spawned",  s24["total_spawned"], sal["total_spawned"]),
            ("✅ Caught",   s24["caught"],         sal["caught"]),
            ("💨 Fled",     s24["fled"],           sal["fled"]),
            ("📈 Rate",
             _catch_rate(s24["caught"], s24["total_spawned"]),
             _catch_rate(sal["caught"], sal["total_spawned"])),
        ]

        # Column widths
        W_LABEL = 12   # label column
        W_COL   = 10   # each data column

        header = f"{'':>{W_LABEL}}  {'24h':^{W_COL}}  {'All Time':^{W_COL}}"
        divider = f"{'─' * W_LABEL}  {'─' * W_COL}  {'─' * W_COL}"
        data_lines = []
        for label, v24, vall in rows:
            data_lines.append(
                f"{label:<{W_LABEL}}  {str(v24):^{W_COL}}  {str(vall):^{W_COL}}"
            )

        table = "\n".join([header, divider] + data_lines)

        reset_line = (
            f"> Window resets <t:{ru}:R>\n"
            f"> <t:{ru}:F>"
        )

        description = (
            f"```\n{table}\n```\n"
            f"{reset_line}"
        )

        embed = discord.Embed(
            title=f"{E.category} {cat['name']}",
            description=description,
            color=discord.Color.blue(),
        )

        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CategoryCog(bot))
