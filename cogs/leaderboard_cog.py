"""
cogs/leaderboard_cog.py  —  Leaderboard commands (last 24 h).

Commands:
  a!leaderboard [category]   — Global or per-category catch leaderboard
"""

import discord
from discord.ext import commands

import db
from categories import get_category, all_keys


class LeaderboardCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="leaderboard", aliases=["lb"])
    async def leaderboard(self, ctx: commands.Context, category: str = None):
        """
        Show the catch leaderboard for the last 24 hours.

        Usage:
          a!leaderboard              — global leaderboard
          a!leaderboard rares        — rares category leaderboard
        """
        guild_id = ctx.guild.id

        if category:
            cat = get_category(category)
            if not cat:
                await ctx.reply(
                    f"❌ Unknown category `{category}`.\n"
                    f"Available: `{'`, `'.join(all_keys())}`"
                )
                return
            entries = await db.get_category_leaderboard(guild_id, cat["pokemon"])
            title   = f"🏆 {cat['name']} Leaderboard — Last 24 Hours"
            lines   = []
            for i, e in enumerate(entries, 1):
                name = await _resolve_name(self.bot, ctx.guild, e["user_id"])
                lines.append(f"`{i:>2}.` **{name}** — {e['total']} caught")
        else:
            entries = await db.get_leaderboard(guild_id)
            title   = "🏆 Global Leaderboard — Last 24 Hours"
            lines   = []
            for i, e in enumerate(entries, 1):
                name      = await _resolve_name(self.bot, ctx.guild, e["user_id"])
                extras    = []
                if e["shiny"]:      extras.append(f"✨{e['shiny']}")
                if e["gigantamax"]: extras.append(f"🔴{e['gigantamax']}")
                extra_str = "  " + "  ".join(extras) if extras else ""
                lines.append(f"`{i:>2}.` **{name}** — {e['total']} caught{extra_str}")

        embed = discord.Embed(
            title=title,
            description="\n".join(lines) if lines else "No catches in the last 24 hours.",
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Data window: last 24 hours")
        await ctx.reply(embed=embed)


async def _resolve_name(bot: discord.Client, guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member:
        return member.display_name
    try:
        user = await bot.fetch_user(user_id)
        return user.display_name
    except Exception:
        return f"<@{user_id}>"


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
