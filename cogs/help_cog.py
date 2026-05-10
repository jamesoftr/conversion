"""
cogs/help_cog.py  —  Custom help command for the Pokémon tracker bot.

Commands
────────
a!help [command]   — Show all commands, or details for a specific one
"""

import discord
from discord.ext import commands


# ── Command reference ─────────────────────────────────────────────────────────
# Each entry: (name, aliases, usage, description, admin_only)

_COMMANDS = [
    # ── Tracker ───────────────────────────────────────────────────────────────
    {
        "name":       "profile",
        "aliases":    ["pf"],
        "usage":      "a!profile [@user]",
        "short":      "Catch profile for the last 24 hours.",
        "long": (
            "Displays catch stats (total, shiny, gigantamax, chain shiny) for yourself "
            "or another member.\n\n"
            "The embed includes buttons for:\n"
            "🔬 **Type Stats** — breakdown of types caught\n"
            "🗺️ **Region Stats** — breakdown by Pokédex region\n"
            "📋 **Pokémon Caught** — paginated list of every species caught"
        ),
        "admin": False,
    },
    {
        "name":       "check",
        "aliases":    [],
        "usage":      "a!check  (reply to a Pokétwo message)",
        "short":      "Manually record a Pokétwo catch or flee.",
        "long": (
            "Reply to any Pokétwo catch congratulations or fled embed message, "
            "then run `a!check` to add it to the database retroactively.\n\n"
            "Useful if the bot was offline when an event occurred."
        ),
        "admin": True,
    },
    {
        "name":       "fled-logs",
        "aliases":    [],
        "usage":      "a!fled-logs <category> <channel_id>\na!fled-logs list",
        "short":      "Configure fled-alert routing per category.",
        "long": (
            "Routes fled-alert embeds for a category to a specific channel.\n\n"
            "`a!fled-logs <category> <channel_id>` — set or update routing\n"
            "`a!fled-logs list` — show the current routing for this server\n\n"
            "Use `a!help catstat` to see available category names."
        ),
        "admin": True,
    },
    {
        "name":       "cleardata",
        "aliases":    [],
        "usage":      "a!cleardata",
        "short":      "Delete all catch & flee data for this server (last 24 h).",
        "long": (
            "Permanently removes every catch and flee record for this server "
            "from the last 24-hour window.\n\n"
            "A confirmation prompt is shown before any data is deleted.\n\n"
            "⚠️ **Bot owner only.**"
        ),
        "admin": True,
    },
    # ── Leaderboard ───────────────────────────────────────────────────────────
    {
        "name":       "leaderboard",
        "aliases":    ["lb"],
        "usage":      "a!leaderboard [category]",
        "short":      "Global or per-category catch leaderboard (last 24 h).",
        "long": (
            "`a!leaderboard` — top 10 catchers server-wide\n"
            "`a!leaderboard <category>` — top 10 catchers for a specific category\n\n"
            "Global entries show shiny (✨) and gigantamax (🔴) bonuses inline."
        ),
        "admin": False,
    },
    # ── Category stats ────────────────────────────────────────────────────────
    {
        "name":       "catstat",
        "aliases":    ["categorystat", "cs"],
        "usage":      "a!catstat <category>",
        "short":      "Spawn / catch / flee stats for a category (last 24 h).",
        "long": (
            "Shows total spawns, catches, flees, and catch rate for every Pokémon "
            "in the given category over the last 24 hours.\n\n"
            f"**Available categories** — check your `categories.py` for the full list, "
            "or run `a!catstat` with no arguments for a reminder."
        ),
        "admin": False,
    },
]

_CMD_INDEX = {c["name"]: c for c in _COMMANDS}
for _c in _COMMANDS:
    for _alias in _c["aliases"]:
        _CMD_INDEX[_alias] = _c


# ── Cog ───────────────────────────────────────────────────────────────────────

class HelpCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="help", aliases=["h"])
    async def help_cmd(self, ctx: commands.Context, command: str = None):
        """
        Show all commands or detailed help for a specific command.

        Usage:
          a!help              — full command list
          a!help profile      — details for a single command
        """
        if command:
            await self._send_command_help(ctx, command.lower())
        else:
            await self._send_overview(ctx)

    # ── Overview embed ────────────────────────────────────────────────────────

    async def _send_overview(self, ctx: commands.Context):
        e = discord.Embed(
            title="📖 Pokémon Tracker — Commands",
            description=(
                "All stats reflect the **last 24 hours** only.\n"
                "Use `a!help <command>` for detailed info on any command.\n"
                "🔒 = requires **Manage Server** permission (or bot owner)"
            ),
            color=discord.Color.gold(),
        )

        sections = {
            "📊 Stats & Profiles": ["profile", "catstat", "leaderboard"],
            "⚙️ Admin":            ["check", "fled-logs", "cleardata"],
        }

        for section, names in sections.items():
            lines = []
            for name in names:
                c = _CMD_INDEX[name]
                alias_str = f"  `{'`, `'.join(c['aliases'])}`" if c["aliases"] else ""
                lock = " 🔒" if c["admin"] else ""
                lines.append(f"`a!{c['name']}`{alias_str}{lock} — {c['short']}")
            e.add_field(name=section, value="\n".join(lines), inline=False)

        e.set_footer(text="Prefix: a!  •  Data window: 24 hours")
        await ctx.reply(embed=e)

    # ── Per-command embed ─────────────────────────────────────────────────────

    async def _send_command_help(self, ctx: commands.Context, command: str):
        c = _CMD_INDEX.get(command)
        if not c:
            await ctx.reply(
                f"❌ Unknown command `{command}`. Run `a!help` to see all commands."
            )
            return

        alias_str = ", ".join(f"`a!{a}`" for a in c["aliases"]) if c["aliases"] else "none"
        e = discord.Embed(
            title=f"📖 a!{c['name']}",
            description=c["long"],
            color=discord.Color.blurple(),
        )
        e.add_field(name="Usage",   value=f"```\n{c['usage']}\n```", inline=False)
        e.add_field(name="Aliases", value=alias_str,                  inline=True)
        e.add_field(name="Access",  value="🔒 Admin / Owner" if c["admin"] else "Everyone", inline=True)
        await ctx.reply(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
