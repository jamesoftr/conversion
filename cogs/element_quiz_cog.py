"""
cogs/element_quiz_cog.py  —  Periodic Table Element Quiz.

How it works
────────────
1.  Counts every non-bot message sent in a guild channel.
2.  Every 10th message triggers an Element Quiz in that same channel.
3.  The bot posts an embed showing the element name with alternating letters
    blanked out (e.g. "N_C_E_" for "Nickel"), plus the symbol and atomic number.
4.  Players guess by just typing the element name anywhere in chat.
5.  The bot scans every message for a matching element name — no command prefix
    needed.  First correct answer wins.
6.  Winner is announced and the active quiz for that guild is cleared.
7.  All state is in-memory (per restart); no DB required.

Commands  (Manage Guild only)
──────────────────────────────
  a!quiz skip          — skip / cancel the current quiz in this guild
  a!quiz status        — show message counter & active quiz info
  a!quiz trigger       — manually fire a quiz right now (for testing)
  a!quiz setchannel    — lock quizzes to a specific channel (optional)
  a!quiz clearchannel  — remove channel restriction
"""

import random
import re

import discord
from discord.ext import commands

from elements import ELEMENTS, get_by_name


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

MESSAGES_PER_QUIZ = 10   # trigger a quiz every N messages


def _mask_name(name: str) -> str:
    """
    Return the element name with every other letter replaced by '_'.
    Spaces and hyphens are kept as-is.
    Example:  "Nickel"  →  "N_C_E_"
              "Iron"    →  "I_O_"
    """
    result = []
    letter_idx = 0
    for ch in name:
        if ch.isalpha():
            result.append(ch.upper() if letter_idx % 2 == 0 else "_")
            letter_idx += 1
        else:
            result.append(ch)   # keep spaces / hyphens intact
    return "".join(result)


def _build_quiz_embed(element: dict) -> discord.Embed:
    masked = _mask_name(element["name"])

    embed = discord.Embed(
        title="🔬 Guess the Element!",
        description=(
            f"```\n{masked}\n```\n"
            f"**Symbol:** `{element['symbol']}`\n"
            f"**Atomic Number:** `{element['atomic_number']}`\n\n"
            f"*Type the element name in chat to answer!*"
        ),
        color=discord.Color.teal(),
    )
    embed.set_footer(text="First correct answer wins • No prefix needed")
    return embed


def _build_win_embed(element: dict, winner: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Correct!",
        description=(
            f"🎉 **{winner.display_name}** got it right!\n\n"
            f"The element was **{element['name']}**\n"
            f"Symbol: `{element['symbol']}` · Atomic #: `{element['atomic_number']}`"
        ),
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=winner.display_avatar.url)
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class ElementQuizCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Per-guild message counters  { guild_id: int }
        self._counters: dict[int, int] = {}

        # Active quiz per guild  { guild_id: {"element": dict, "channel_id": int} }
        self._active: dict[int, dict] = {}

        # Optional channel restriction per guild  { guild_id: int | None }
        self._quiz_channel: dict[int, int | None] = {}

        # Build a flat set of all element names (lowercase) for fast scanning
        self._all_names_lower: set[str] = {e["name"].lower() for e in ELEMENTS}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _increment(self, guild_id: int) -> int:
        """Bump the message counter and return the new value."""
        self._counters[guild_id] = self._counters.get(guild_id, 0) + 1
        return self._counters[guild_id]

    def _reset_counter(self, guild_id: int) -> None:
        self._counters[guild_id] = 0

    def _pick_element(self) -> dict:
        return random.choice(ELEMENTS)

    def _find_element_in_text(self, text: str) -> dict | None:
        """
        Scan `text` for any element name (whole-word, case-insensitive).
        Returns the matching element dict or None.
        """
        text_lower = text.lower()
        # Sort by length descending so longer names match before shorter subsets
        for name_lower in sorted(self._all_names_lower, key=len, reverse=True):
            # Use word-boundary regex to avoid partial matches (e.g. "iron" in "environment")
            pattern = rf"\b{re.escape(name_lower)}\b"
            if re.search(pattern, text_lower):
                return get_by_name(name_lower)
        return None

    async def _post_quiz(self, channel: discord.TextChannel, guild_id: int) -> None:
        element = self._pick_element()
        self._active[guild_id] = {"element": element, "channel_id": channel.id}
        await channel.send(embed=_build_quiz_embed(element))

    # ── Listener ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore DMs, bots, and system messages
        if not message.guild or message.author.bot or not message.content:
            return

        guild_id = message.guild.id

        # ── Check for an active quiz answer ──────────────────────────────────
        active = self._active.get(guild_id)
        if active:
            found = self._find_element_in_text(message.content)
            if found:
                correct = active["element"]
                if found["name"].lower() == correct["name"].lower():
                    # Winner!
                    self._active.pop(guild_id, None)
                    self._reset_counter(guild_id)
                    await message.channel.send(
                        embed=_build_win_embed(correct, message.author)
                    )
                    return   # don't count this message toward next quiz
                # Wrong element named — silently ignore, keep quiz alive

        # ── Count toward next quiz trigger ────────────────────────────────────
        # Respect optional channel lock
        locked_channel = self._quiz_channel.get(guild_id)
        if locked_channel and message.channel.id != locked_channel:
            return   # this channel doesn't count

        count = self._increment(guild_id)
        if count >= MESSAGES_PER_QUIZ and guild_id not in self._active:
            self._reset_counter(guild_id)
            await self._post_quiz(message.channel, guild_id)

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.group(name="quiz", invoke_without_command=True)
    async def quiz(self, ctx: commands.Context):
        """Element Quiz management commands."""
        await ctx.send_help(ctx.command)

    @quiz.command(name="skip")
    @commands.has_permissions(manage_guild=True)
    async def quiz_skip(self, ctx: commands.Context):
        """Skip / cancel the currently active element quiz in this guild."""
        active = self._active.pop(ctx.guild.id, None)
        if active:
            name = active["element"]["name"]
            await ctx.reply(f"⏭️ Quiz skipped. The element was **{name}**.")
        else:
            await ctx.reply("ℹ️ No active quiz to skip right now.")

    @quiz.command(name="status")
    async def quiz_status(self, ctx: commands.Context):
        """Show the current message counter and quiz state for this guild."""
        guild_id = ctx.guild.id
        count   = self._counters.get(guild_id, 0)
        active  = self._active.get(guild_id)
        locked  = self._quiz_channel.get(guild_id)

        lines = [
            f"**Messages until next quiz:** `{MESSAGES_PER_QUIZ - count}` "
            f"*(counter: {count}/{MESSAGES_PER_QUIZ})*",
        ]
        if active:
            lines.append(
                f"**Active quiz:** `{_mask_name(active['element']['name'])}` "
                f"in <#{active['channel_id']}>"
            )
        else:
            lines.append("**Active quiz:** None")

        if locked:
            lines.append(f"**Quiz channel:** <#{locked}>")
        else:
            lines.append("**Quiz channel:** Any (wherever 10th message lands)")

        embed = discord.Embed(
            title="🔬 Element Quiz Status",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    @quiz.command(name="trigger")
    @commands.has_permissions(manage_guild=True)
    async def quiz_trigger(self, ctx: commands.Context):
        """Manually fire an element quiz right now (useful for testing)."""
        guild_id = ctx.guild.id
        if guild_id in self._active:
            await ctx.reply("⚠️ A quiz is already active! Use `a!quiz skip` first.")
            return
        self._reset_counter(guild_id)
        await self._post_quiz(ctx.channel, guild_id)

    @quiz.command(name="setchannel")
    @commands.has_permissions(manage_guild=True)
    async def quiz_setchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """
        Lock quizzes to a specific channel.
        Only messages in that channel will count toward the quiz trigger.

        Usage: a!quiz setchannel #general
        """
        target = channel or ctx.channel
        self._quiz_channel[ctx.guild.id] = target.id
        await ctx.reply(f"✅ Element quizzes will now only trigger in {target.mention}.")

    @quiz.command(name="clearchannel")
    @commands.has_permissions(manage_guild=True)
    async def quiz_clearchannel(self, ctx: commands.Context):
        """Remove the channel restriction — quizzes trigger wherever the 10th message lands."""
        self._quiz_channel.pop(ctx.guild.id, None)
        await ctx.reply("✅ Channel restriction removed. Quizzes can now trigger in any channel.")

    # ── Error handler ─────────────────────────────────────────────────────────

    @quiz.error
    async def quiz_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(ElementQuizCog(bot))
