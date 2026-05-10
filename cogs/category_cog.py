"""
cogs/category_cog.py  —  Per-category stats (last 24 h).

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
        Show spawn / catch / flee statistics for a category (last 24 hours).

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

        stats   = await db.get_category_stats(ctx.guild.id, cat["pokemon"])
        caught  = stats["caught"]
        fled    = stats["fled"]
        spawned = stats["total_spawned"]
        rate    = f"{caught / spawned * 100:.1f}%" if spawned else "N/A"

        e = discord.Embed(
            title=f"📊 {cat['name']} — Last 24 Hours",
            color=discord.Color.blue(),
        )
        e.add_field(name="Total Spawned", value=str(spawned), inline=True)
        e.add_field(name="✅ Caught",     value=str(caught),  inline=True)
        e.add_field(name="💨 Fled",       value=str(fled),    inline=True)
        e.add_field(name="Catch Rate",    value=rate,         inline=True)
        e.set_footer(text="Data window: last 24 hours")

        await ctx.reply(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(CategoryCog(bot))
