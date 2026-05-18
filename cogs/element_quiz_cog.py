"""
cogs/element_quiz_cog.py  —  Periodic Table Element Quiz.

How it works
────────────
1.  Counts every non-bot message sent in a guild channel (or DM).
2.  Every 10th message triggers an Element Quiz in that same channel.
3.  The bot posts an embed showing the element name with alternating letters
    blanked out (e.g. "N_C_E_" for "Nickel"), plus the symbol and atomic number.
4.  Players guess by just typing the element name anywhere in chat.
5.  The bot scans every message for a matching element name — no command prefix
    needed.  First correct answer wins.
6.  Winner is announced with their updated total score. The quiz is then cleared.
7.  If no one guesses within QUIZ_TIMEOUT_SECONDS, the bot reveals the answer.
8.  All state is in-memory (resets on restart); no DB required.
9.  Messages starting with a Pokétwo ping are ignored entirely.
10. DMs work — each user gets their own independent quiz session.

State keys
──────────
  Guild messages  →  key = guild_id        (int)
  DM messages     →  key = f"dm_{user_id}" (str)

Commands  (Manage Guild only — guild only, not usable in DMs)
──────────────────────────────────────────────────────────────
  a!quiz skip          — skip / cancel the current quiz
  a!quiz status        — show message counter & active quiz info
  a!quiz trigger       — manually fire a quiz right now (for testing)
  a!quiz setchannel    — lock quizzes to a specific channel (optional, guild only)
  a!quiz clearchannel  — remove channel restriction
  a!quiz scores        — show the element quiz leaderboard
"""

import asyncio
import random
import re

import discord
from discord.ext import commands

from elements import ELEMENTS, get_by_name


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MESSAGES_PER_QUIZ    = 10
POKETWO_ID           = 716390085896962058
QUIZ_TIMEOUT_SECONDS = 120   # seconds before the quiz auto-expires with no winner
LEADERBOARD_SIZE     = 10   # max entries shown in the scores embed


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mask_name(name: str) -> str:
    """
    Return the element name with every other letter replaced by '_'.
    Spaces and hyphens are kept as-is.
    Example:  "Nickel"       →  "N_C_E_"
              "Iron"         →  "I_O_"
              "Einsteinium"  →  "E_N_T_I_I_M"
    """
    result     = []
    letter_idx = 0
    for ch in name:
        if ch.isalpha():
            result.append(ch.upper() if letter_idx % 2 == 0 else "_")
            letter_idx += 1
        else:
            result.append(ch)
    return "".join(result)


def _build_quiz_embed(element: dict) -> discord.Embed:
    masked = _mask_name(element["name"])
    embed  = discord.Embed(
        title="🔬 Guess the Element!",
        description=(
            f"```\n{masked}\n```\n"
            f"**Symbol:** `{element['symbol']}`\n"
            f"**Atomic Number:** `{element['atomic_number']}`\n\n"
            f"*Type the element name in chat to answer!*"
        ),
        color=discord.Color.teal(),
    )
    embed.set_footer(text=f"First correct answer wins • No prefix needed • {QUIZ_TIMEOUT_SECONDS}s to answer")
    return embed


def _build_win_embed(
    element: dict,
    winner: discord.User | discord.Member,
    new_total: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Correct!",
        description=(
            f"🎉 **{winner.display_name}** got it right!\n\n"
            f"The element was **{element['name']}**\n"
            f"Symbol: `{element['symbol']}` · Atomic #: `{element['atomic_number']}`\n\n"
            f"**{winner.display_name}'s total correct guesses:** `{new_total}` 🏆\n\n"
            f"*Check the leaderboard with `a!quiz scores`*"
        ),
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=winner.display_avatar.url)
    return embed


def _build_timeout_embed(element: dict) -> discord.Embed:
    embed = discord.Embed(
        title="⏰ Time's Up!",
        description=(
            f"Nobody guessed in time!\n\n"
            f"The element was **{element['name']}**\n"
            f"Symbol: `{element['symbol']}` · Atomic #: `{element['atomic_number']}`"
        ),
        color=discord.Color.red(),
    )
    embed.set_footer(text="Better luck next time!")
    return embed


def _build_leaderboard_embed(
    scores: dict[int, int],
    bot: commands.Bot,
    scope_label: str,
) -> discord.Embed:
    """
    Build a leaderboard embed from a {user_id: score} dict.
    Resolves display names via bot.get_user(); falls back to 'Unknown User'.
    """
    if not scores:
        embed = discord.Embed(
            title="🏆 Element Quiz Leaderboard",
            description="No scores yet — be the first to guess correctly!",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=scope_label)
        return embed

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top           = sorted_scores[:LEADERBOARD_SIZE]

    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for rank, (user_id, score) in enumerate(top, start=1):
        user   = bot.get_user(user_id)
        name   = user.display_name if user else f"Unknown User ({user_id})"
        medal  = medals[rank - 1] if rank <= 3 else f"`#{rank}`"
        lines.append(f"{medal} **{name}** — `{score}` correct")

    embed = discord.Embed(
        title="🏆 Element Quiz Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=scope_label)
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class ElementQuizCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # { scope_key: int }   — message counters
        self._counters: dict[int | str, int] = {}

        # { scope_key: {"element": dict, "channel_id": int} }
        self._active: dict[int | str, dict] = {}

        # { scope_key: int | None }  — optional channel lock (guilds only)
        self._quiz_channel: dict[int, int | None] = {}

        # Leaderboard — guild scores:  { guild_id: { user_id: int } }
        #             — DM scores:     { f"dm_{user_id}": { user_id: int } }
        self._scores: dict[int | str, dict[int, int]] = {}

        # Timeout tasks — { scope_key: asyncio.Task }
        self._timeout_tasks: dict[int | str, asyncio.Task] = {}

        # Sorted element names longest-first for greedy matching
        self._sorted_names: list[str] = sorted(
            (e["name"].lower() for e in ELEMENTS), key=len, reverse=True
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _scope_key(self, message: discord.Message) -> int | str:
        """Return a unique state key for the message's scope."""
        if message.guild:
            return message.guild.id
        return f"dm_{message.author.id}"

    def _increment(self, key: int | str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def _reset_counter(self, key: int | str) -> None:
        self._counters[key] = 0

    def _add_score(self, key: int | str, user_id: int) -> int:
        """Increment the score for user_id in the given scope. Returns new total."""
        scope_scores        = self._scores.setdefault(key, {})
        scope_scores[user_id] = scope_scores.get(user_id, 0) + 1
        return scope_scores[user_id]

    def _cancel_timeout(self, key: int | str) -> None:
        task = self._timeout_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _find_element_in_text(self, text: str) -> dict | None:
        """
        Scan text for any element name (whole-word, case-insensitive).
        Longer names are checked first to avoid "tin" matching inside "titanium".
        """
        text_lower = text.lower()
        for name_lower in self._sorted_names:
            pattern = rf"\b{re.escape(name_lower)}\b"
            if re.search(pattern, text_lower):
                return get_by_name(name_lower)
        return None

    async def _quiz_timeout(
        self,
        key: int | str,
        channel: discord.TextChannel | discord.DMChannel,
    ) -> None:
        """
        Wait QUIZ_TIMEOUT_SECONDS, then reveal the answer if no one has guessed.
        This runs as a background task and cancels itself cleanly if the quiz
        is already resolved (correct guess or manual skip).
        """
        await asyncio.sleep(QUIZ_TIMEOUT_SECONDS)

        # If quiz is still active for this scope, reveal the answer
        active = self._active.pop(key, None)
        self._timeout_tasks.pop(key, None)
        if active:
            self._reset_counter(key)
            try:
                await channel.send(embed=_build_timeout_embed(active["element"]))
            except discord.HTTPException:
                pass  # channel may have been deleted, etc.

    async def _post_quiz(
        self,
        channel: discord.TextChannel | discord.DMChannel,
        key: int | str,
    ) -> None:
        element           = random.choice(ELEMENTS)
        self._active[key] = {"element": element, "channel_id": channel.id}

        # Cancel any stale timeout (shouldn't happen, but be safe)
        self._cancel_timeout(key)

        await channel.send(embed=_build_quiz_embed(element))

        # Start the timeout countdown
        task = asyncio.get_event_loop().create_task(
            self._quiz_timeout(key, channel)
        )
        self._timeout_tasks[key] = task

    # ── Listener ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots and empty messages
        if message.author.bot or not message.content:
            return

        # Ignore messages that are a Pokétwo ping
        if message.content.startswith(f"<@{POKETWO_ID}>"):
            return

        # Only handle guild text channels and DMs
        is_dm    = isinstance(message.channel, discord.DMChannel)
        is_guild = bool(message.guild)
        if not is_dm and not is_guild:
            return

        key = self._scope_key(message)

        # ── Check for active quiz answer ──────────────────────────────────────
        active = self._active.get(key)
        if active:
            found = self._find_element_in_text(message.content)
            if found and found["name"].lower() == active["element"]["name"].lower():
                # Correct! Clear quiz and timeout before doing anything else
                self._active.pop(key, None)
                self._cancel_timeout(key)
                self._reset_counter(key)

                new_total = self._add_score(key, message.author.id)

                await message.channel.send(
                    embed=_build_win_embed(active["element"], message.author, new_total)
                )
                return  # don't count this message toward the next quiz

        # ── Count toward next quiz trigger ────────────────────────────────────
        # Guild: respect optional channel lock
        if is_guild:
            locked_channel = self._quiz_channel.get(message.guild.id)
            if locked_channel and message.channel.id != locked_channel:
                return  # this channel doesn't count

        count = self._increment(key)
        if count >= MESSAGES_PER_QUIZ and key not in self._active:
            self._reset_counter(key)
            await self._post_quiz(message.channel, key)

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.group(name="quiz", invoke_without_command=True)
    async def quiz(self, ctx: commands.Context):
        """Element Quiz management commands."""
        await ctx.send_help(ctx.command)

    def _is_admin(self, ctx: commands.Context) -> bool:
        """True if the user has Manage Guild in a server, or is in DMs (no guild = no restriction)."""
        if ctx.guild is None:
            return True  # DMs — allow freely
        return ctx.author.guild_permissions.manage_guild

    @quiz.command(name="skip")
    async def quiz_skip(self, ctx: commands.Context):
        """Skip / cancel the currently active element quiz."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
            return
        key    = self._scope_key(ctx.message)
        self._cancel_timeout(key)
        active = self._active.pop(key, None)
        if active:
            await ctx.reply(f"⏭️ Quiz skipped. The element was **{active['element']['name']}**.")
        else:
            await ctx.reply("ℹ️ No active quiz to skip right now.")

    @quiz.command(name="status")
    async def quiz_status(self, ctx: commands.Context):
        """Show the current message counter and quiz state."""
        key    = self._scope_key(ctx.message)
        count  = self._counters.get(key, 0)
        active = self._active.get(key)

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

        if ctx.guild:
            locked = self._quiz_channel.get(ctx.guild.id)
            lines.append(
                f"**Quiz channel:** <#{locked}>" if locked
                else "**Quiz channel:** Any (wherever 10th message lands)"
            )
        else:
            lines.append("**Scope:** DM (your personal quiz session)")

        embed = discord.Embed(
            title="🔬 Element Quiz Status",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    @quiz.command(name="trigger")
    async def quiz_trigger(self, ctx: commands.Context):
        """Manually fire an element quiz right now (useful for testing)."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
            return
        key = self._scope_key(ctx.message)
        if key in self._active:
            await ctx.reply("⚠️ A quiz is already active! Use `a!quiz skip` first.")
            return
        self._reset_counter(key)
        await self._post_quiz(ctx.channel, key)

    @quiz.command(name="setchannel")
    async def quiz_setchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """
        Lock quizzes to a specific channel (guild only).
        Only messages in that channel will count toward the quiz trigger.

        Usage: a!quiz setchannel #general
        """
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
            return
        if not ctx.guild:
            await ctx.reply("ℹ️ Channel locking only applies in servers, not DMs.")
            return
        target = channel or ctx.channel
        self._quiz_channel[ctx.guild.id] = target.id
        await ctx.reply(f"✅ Element quizzes will now only trigger in {target.mention}.")

    @quiz.command(name="clearchannel")
    async def quiz_clearchannel(self, ctx: commands.Context):
        """Remove the channel restriction — quizzes trigger wherever the 10th message lands."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
            return
        if not ctx.guild:
            await ctx.reply("ℹ️ Channel locking only applies in servers, not DMs.")
            return
        self._quiz_channel.pop(ctx.guild.id, None)
        await ctx.reply("✅ Channel restriction removed. Quizzes can now trigger in any channel.")

    @quiz.command(name="scores")
    async def quiz_scores(self, ctx: commands.Context):
        """Show the element quiz leaderboard for this server (or your DM session)."""
        key          = self._scope_key(ctx.message)
        scope_scores = self._scores.get(key, {})

        if ctx.guild:
            scope_label = f"Server: {ctx.guild.name}"
        else:
            scope_label = "Your personal DM quiz session"

        embed = _build_leaderboard_embed(scope_scores, self.bot, scope_label)
        await ctx.reply(embed=embed)

    # ── Error handler ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        # Only handle errors from this cog's commands
        if ctx.command is None or ctx.command.cog is not self:
            return
        # Unwrap CheckFailure (e.g. has_permissions failing in DMs)
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
        elif isinstance(error, commands.CommandNotFound):
            pass  # ignore unknown subcommands silently
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(ElementQuizCog(bot))
