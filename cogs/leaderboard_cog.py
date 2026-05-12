"""
cogs/leaderboard_cog.py  —  Leaderboard commands.

Shows BOTH last-24-hour and all-time totals in every leaderboard embed,
plus a footer countdown for how long until the 24-h window rolls forward.

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
        Show the catch leaderboard — last 24 hours alongside all-time totals.

        Usage:
          a!leaderboard              — global leaderboard
          a!leaderboard rares        — rares category leaderboard
        """
        guild_id   = ctx.guild.id
        reset_info = await db.get_window_reset_info(guild_id)
        reset_str  = db.fmt_reset(reset_info["resets_in_h"])

        if category:
            cat = get_category(category)
            if not cat:
                await ctx.reply(
                    f"❌ Unknown category `{category}`.\n"
                    f"Available: `{'`, `'.join(all_keys())}`"
                )
                return

            entries_24h = await db.get_category_leaderboard(guild_id, cat["pokemon"])
            entries_all = await db.get_category_leaderboard_alltime(guild_id, cat["pokemon"])

            # Build a combined lookup: user_id → {24h, alltime}
            all_map = {e["user_id"]: e["total"] for e in entries_all}

            title = f"🏆 {cat['name']} Leaderboard"
            lines = []
            for i, e in enumerate(entries_24h, 1):
                name     = await _resolve_name(self.bot, ctx.guild, e["user_id"])
                alltime  = all_map.get(e["user_id"], e["total"])
                lines.append(
                    f"`{i:>2}.` **{name}** — {e['total']} caught *(all-time: {alltime})*"
                )

            # If 24-h board is empty, show all-time only
            if not lines:
                for i, e in enumerate(entries_all, 1):
                    name = await _resolve_name(self.bot, ctx.guild, e["user_id"])
                    lines.append(f"`{i:>2}.` **{name}** — 0 (24 h) · {e['total']} all-time")

        else:
            entries_24h = await db.get_leaderboard(guild_id)
            entries_all = await db.get_leaderboard_alltime(guild_id)

            all_map = {e["user_id"]: e for e in entries_all}

            title = "🏆 Global Leaderboard"
            lines = []
            for i, e in enumerate(entries_24h, 1):
                name    = await _resolve_name(self.bot, ctx.guild, e["user_id"])
                extras  = []
                if e["shiny"]:      extras.append(f"✨{e['shiny']}")
                if e["gigantamax"]: extras.append(f"🔴{e['gigantamax']}")
                extra_str = "  " + "  ".join(extras) if extras else ""

                at      = all_map.get(e["user_id"], {})
                alltime = at.get("total", e["total"])
                lines.append(
                    f"`{i:>2}.` **{name}** — {e['total']} caught{extra_str} *(all-time: {alltime})*"
                )

            if not lines:
                for i, e in enumerate(entries_all, 1):
                    name    = await _resolve_name(self.bot, ctx.guild, e["user_id"])
                    extras  = []
                    if e["shiny"]:      extras.append(f"✨{e['shiny']}")
                    if e["gigantamax"]: extras.append(f"🔴{e['gigantamax']}")
                    extra_str = "  " + "  ".join(extras) if extras else ""
                    lines.append(
                        f"`{i:>2}.` **{name}** — 0 (24 h) · {e['total']} all-time{extra_str}"
                    )

        embed = discord.Embed(
            title=title,
            description="\n".join(lines) if lines else "No catches recorded yet.",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"24-hour window · {reset_str}")
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
