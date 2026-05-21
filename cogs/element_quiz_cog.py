"""
cogs/element_quiz_cog.py  —  Periodic Table Element Quiz.

How it works
────────────
1.  Counts every non-bot message sent in a guild channel (or DM).
2.  Every 10th message triggers a randomly chosen Element Quiz in that channel.
3.  Three quiz types are randomly selected each round:
      • NAME   — masked element name shown; type the answer in chat
      • SYMBOL — element symbol shown; pick from 4 buttons
      • ATOMIC — atomic number shown; pick from 4 buttons
4.  First correct answer wins; wrong button clicks are penalised with a 5s cooldown.
5.  Winner is announced with their updated total score. The quiz is then cleared.
6.  If no one answers within QUIZ_TIMEOUT_SECONDS, the bot reveals the answer.
7.  Scores are persisted to MongoDB (quiz_scores collection).
8.  Messages starting with a Pokétwo ping are ignored entirely.
9.  DMs work — each user gets their own independent quiz session.

State keys
──────────
  Guild messages  →  key = guild_id        (int)
  DM messages     →  key = f"dm_{user_id}" (str)

Commands  (Manage Guild only — guild only)
──────────────────────────────────────────
  a!quiz skip          — skip / cancel the current quiz
  a!quiz status        — show message counter & active quiz info
  a!quiz trigger       — manually fire a quiz right now (for testing)
  a!quiz setchannel    — lock quizzes to a specific channel
  a!quiz clearchannel  — remove channel restriction
  a!quiz scores        — show the element quiz leaderboard
"""

import asyncio
import random
import re
from enum import Enum

import discord
from discord.ext import commands

from elements import ELEMENTS, get_by_name
import db as _db


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MESSAGES_PER_QUIZ    = 10
POKETWO_ID           = 716390085896962058
QUIZ_TIMEOUT_SECONDS = 120
LEADERBOARD_SIZE     = 10
INCENSE_INTERVAL     = 20
WRONG_COOLDOWN       = 5    # seconds a user must wait after a wrong button click

# Set to a channel ID to auto-start incense on bot startup, or None to disable.
INCENSE_AUTO_CHANNEL_ID: int | None = 1506869977636933743  # e.g. 123456789012345678
# Interval (seconds) used specifically for the auto-startup incense session.
# The normal a!quiz incense command still uses INCENSE_INTERVAL.
INCENSE_AUTO_INTERVAL    = 30


class QuizType(Enum):
    NAME   = "name"    # masked name — type in chat
    SYMBOL = "symbol"  # given symbol — 4 buttons
    ATOMIC = "atomic"  # given atomic number — 4 buttons


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mask_name(name: str) -> str:
    """Every other letter replaced by '_'. Spaces/hyphens kept."""
    result, letter_idx = [], 0
    for ch in name:
        if ch.isalpha():
            result.append(ch.upper() if letter_idx % 2 == 0 else "_")
            letter_idx += 1
        else:
            result.append(ch)
    return "".join(result)


def _pick_choices(correct: dict) -> list[dict]:
    """Return [correct] + 3 random wrong elements, shuffled."""
    pool    = [e for e in ELEMENTS if e["name"] != correct["name"]]
    choices = random.sample(pool, 3) + [correct]
    random.shuffle(choices)
    return choices


# ── Embeds ────────────────────────────────────────────────────────────────────

def _build_name_embed(element: dict) -> discord.Embed:
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


def _build_symbol_embed(element: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🔣 Guess the Element!",
        description=(
            f"**Which element has the symbol:**\n"
            f"```\n{element['symbol']}\n```\n"
            f"**Atomic Number:** `{element['atomic_number']}`\n\n"
            f"*Click the correct button below!*"
        ),
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"First correct click wins • {QUIZ_TIMEOUT_SECONDS}s to answer • Wrong = {WRONG_COOLDOWN}s cooldown")
    return embed


def _build_atomic_embed(element: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🔢 Guess the Element!",
        description=(
            f"**Which element has atomic number:**\n"
            f"```\n{element['atomic_number']}\n```\n"
            f"*Click the correct button below!*"
        ),
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"First correct click wins • {QUIZ_TIMEOUT_SECONDS}s to answer • Wrong = {WRONG_COOLDOWN}s cooldown")
    return embed


def _build_win_embed(
    element: dict,
    winner:  discord.User | discord.Member,
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
            f"Nobody answered in time!\n\n"
            f"The element was **{element['name']}**\n"
            f"Symbol: `{element['symbol']}` · Atomic #: `{element['atomic_number']}`"
        ),
        color=discord.Color.red(),
    )
    embed.set_footer(text="Better luck next time!")
    return embed


def _build_incense_embed(element: dict, quiz_type: QuizType, last_name: str | None) -> discord.Embed:
    prev = f"Last one was **{last_name}**. " if last_name else ""

    if quiz_type == QuizType.SYMBOL:
        desc = (
            f"{prev}Guess the new one!\n\n"
            f"**Which element has the symbol:**\n"
            f"```\n{element['symbol']}\n```\n"
            f"**Atomic Number:** `{element['atomic_number']}`\n\n"
            f"*Click the correct button below!*"
        )
        color = discord.Color.blue()
        title = "🧪 Incense — Symbol Quiz!"
    elif quiz_type == QuizType.ATOMIC:
        desc = (
            f"{prev}Guess the new one!\n\n"
            f"**Which element has atomic number:**\n"
            f"```\n{element['atomic_number']}\n```\n"
            f"*Click the correct button below!*"
        )
        color = discord.Color.orange()
        title = "🧪 Incense — Atomic Number Quiz!"
    else:
        masked = _mask_name(element["name"])
        desc = (
            f"{prev}Guess the new one!\n\n"
            f"```\n{masked}\n```\n"
            f"**Symbol:** `{element['symbol']}`\n"
            f"**Atomic Number:** `{element['atomic_number']}`\n\n"
            f"*Type the element name in chat to answer!*"
        )
        color = discord.Color.purple()
        title = "🧪 Incense — Name Quiz!"

    embed = discord.Embed(title=title, description=desc, color=color)
    embed.set_footer(text=f"Next element in {INCENSE_INTERVAL}s • Use `a!quiz incense stop` to end")
    return embed


async def _build_leaderboard_embed(
    scores: dict[int, int],
    bot: commands.Bot,
    scope_label: str,
) -> discord.Embed:
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
    medals        = ["🥇", "🥈", "🥉"]
    lines         = []

    for rank, (user_id, score) in enumerate(top, start=1):
        user = bot.get_user(user_id)
        if user is None:
            try:
                user = await bot.fetch_user(user_id)
            except discord.NotFound:
                user = None
        name  = user.display_name if user else "Unknown User"
        medal = medals[rank - 1] if rank <= 3 else f"`#{rank}`"
        lines.append(f"{medal} **{name}** <@{user_id}> — `{score}` correct")

    embed = discord.Embed(
        title="🏆 Element Quiz Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=scope_label)
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Multi-choice button view (SYMBOL / ATOMIC quiz types)
# ─────────────────────────────────────────────────────────────────────────────

class MultiChoiceView(discord.ui.View):
    """
    Four buttons for SYMBOL / ATOMIC quizzes.
    Correct click → fires on_correct callback, disables all buttons green/red.
    Wrong click   → ephemeral penalty message + WRONG_COOLDOWN cooldown for that user.
    Timeout       → disables all buttons, fires on_timeout callback.
    """

    def __init__(
        self,
        correct:    dict,
        choices:    list[dict],
        on_correct,          # async callable(interaction, element)
        on_timeout,          # async callable()
    ):
        super().__init__(timeout=QUIZ_TIMEOUT_SECONDS)
        self._correct    = correct
        self._on_correct = on_correct
        self._on_timeout = on_timeout
        self._cooldowns: dict[int, float] = {}  # user_id → when cooldown expires
        self._resolved   = False

        for choice in choices:
            btn = discord.ui.Button(
                label=choice["name"],
                style=discord.ButtonStyle.primary,
                custom_id=choice["name"],
            )
            btn.callback = self._make_callback(choice)
            self.add_item(btn)

    def _make_callback(self, choice: dict):
        async def callback(interaction: discord.Interaction):
            if self._resolved:
                await interaction.response.send_message(
                    "⚡ This quiz has already ended!", ephemeral=True
                )
                return

            # Cooldown check
            import time
            now = time.monotonic()
            until = self._cooldowns.get(interaction.user.id, 0)
            if now < until:
                remaining = round(until - now, 1)
                await interaction.response.send_message(
                    f"⏳ You're on cooldown! Try again in **{remaining}s**.",
                    ephemeral=True,
                )
                return

            if choice["name"] == self._correct["name"]:
                self._resolved = True
                self._colour_buttons()
                await interaction.response.edit_message(view=self)
                await self._on_correct(interaction, self._correct)
            else:
                self._cooldowns[interaction.user.id] = now + WRONG_COOLDOWN
                await interaction.response.send_message(
                    f"❌ **{choice['name']}** is wrong! Wait **{WRONG_COOLDOWN}s** before trying again.",
                    ephemeral=True,
                )

        return callback

    def _colour_buttons(self):
        """Turn correct button green, all others red, and disable everything."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == self._correct["name"]:
                    item.style    = discord.ButtonStyle.success
                else:
                    item.style    = discord.ButtonStyle.danger
                item.disabled = True

    async def on_timeout(self):
        if self._resolved:
            return
        self._resolved = True
        self._colour_buttons()
        await self._on_timeout()


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class ElementQuizCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # { scope_key: int }
        self._counters: dict[int | str, int] = {}

        # { scope_key: {"element": dict, "channel_id": int, "quiz_type": QuizType} }
        self._active: dict[int | str, dict] = {}

        # { scope_key: int | None }  — optional channel lock
        self._quiz_channel: dict[int, int | None] = {}

        # Timeout tasks — { scope_key: asyncio.Task }
        self._timeout_tasks: dict[int | str, asyncio.Task] = {}

        # Incense sessions — { channel_id: asyncio.Task }
        self._incense_tasks: dict[int, asyncio.Task] = {}
        # Last element name shown by incense per channel
        self._incense_last:  dict[int, str] = {}
        # Current incense element per channel (NAME type only — buttons handle themselves)
        self._incense_active: dict[int, dict] = {}

        # Sorted element names longest-first for greedy matching
        self._sorted_names: list[str] = sorted(
            (e["name"].lower() for e in ELEMENTS), key=len, reverse=True
        )

    # ── Startup ───────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        if not INCENSE_AUTO_CHANNEL_ID:
            return

        async def _auto_start():
            await self.bot.wait_until_ready()
            channel = self.bot.get_channel(INCENSE_AUTO_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(INCENSE_AUTO_CHANNEL_ID)
                except discord.HTTPException:
                    print(f"[ElementQuiz] Auto-incense: could not find channel {INCENSE_AUTO_CHANNEL_ID}")
                    return
            if channel.id in self._incense_tasks:
                return
            scope_key = channel.guild.id if hasattr(channel, "guild") and channel.guild else f"dm_{INCENSE_AUTO_CHANNEL_ID}"
            task = asyncio.get_event_loop().create_task(
                self._incense_loop(channel, scope_key, interval=INCENSE_AUTO_INTERVAL)
            )
            self._incense_tasks[channel.id] = task
            print(f"[ElementQuiz] Auto-incense started in #{channel.name} ({channel.id})")

        asyncio.get_event_loop().create_task(_auto_start())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _scope_key(self, message: discord.Message) -> int | str:
        if message.guild:
            return message.guild.id
        return f"dm_{message.author.id}"

    def _increment(self, key: int | str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def _reset_counter(self, key: int | str) -> None:
        self._counters[key] = 0

    async def _add_score(self, key: int | str, user_id: int) -> int:
        return await _db.quiz_add_score(str(key), user_id)

    def _cancel_timeout(self, key: int | str) -> None:
        task = self._timeout_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _find_element_in_text(self, text: str) -> dict | None:
        text_lower = text.lower()
        for name_lower in self._sorted_names:
            if re.search(rf"\b{re.escape(name_lower)}\b", text_lower):
                return get_by_name(name_lower)
        return None

    async def _quiz_timeout(
        self,
        key: int | str,
        channel: discord.TextChannel | discord.DMChannel,
    ) -> None:
        """Reveal answer if NAME quiz times out (button quizzes handle timeout via the View)."""
        await asyncio.sleep(QUIZ_TIMEOUT_SECONDS)
        active = self._active.pop(key, None)
        self._timeout_tasks.pop(key, None)
        if active and active.get("quiz_type") == QuizType.NAME:
            self._reset_counter(key)
            try:
                await channel.send(embed=_build_timeout_embed(active["element"]))
            except discord.HTTPException:
                pass

    async def _post_quiz(
        self,
        channel: discord.TextChannel | discord.DMChannel,
        key: int | str,
        quiz_type: QuizType | None = None,
    ) -> None:
        element   = random.choice(ELEMENTS)
        quiz_type = quiz_type or random.choice(list(QuizType))

        self._active[key] = {
            "element":   element,
            "channel_id": channel.id,
            "quiz_type": quiz_type,
        }
        self._cancel_timeout(key)

        if quiz_type == QuizType.NAME:
            await channel.send(embed=_build_name_embed(element))
            # Start text-answer timeout
            task = asyncio.get_event_loop().create_task(
                self._quiz_timeout(key, channel)
            )
            self._timeout_tasks[key] = task

        else:
            choices = _pick_choices(element)
            embed   = (
                _build_symbol_embed(element)
                if quiz_type == QuizType.SYMBOL
                else _build_atomic_embed(element)
            )

            async def on_correct(interaction: discord.Interaction, el: dict):
                self._active.pop(key, None)
                self._cancel_timeout(key)
                self._reset_counter(key)
                new_total = await self._add_score(key, interaction.user.id)
                await interaction.followup.send(
                    embed=_build_win_embed(el, interaction.user, new_total)
                )

            async def on_timeout():
                active = self._active.pop(key, None)
                self._cancel_timeout(key)
                if active:
                    self._reset_counter(key)
                    try:
                        await channel.send(embed=_build_timeout_embed(active["element"]))
                    except discord.HTTPException:
                        pass

            view = MultiChoiceView(
                correct=element,
                choices=choices,
                on_correct=on_correct,
                on_timeout=on_timeout,
            )
            msg = await channel.send(embed=embed, view=view)
            # Store message ref so skip can edit it
            self._active[key]["view_message"] = msg

    # ── Listener ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content:
            return
        if message.content.startswith(f"<@{POKETWO_ID}>"):
            return

        is_dm    = isinstance(message.channel, discord.DMChannel)
        is_guild = bool(message.guild)
        if not is_dm and not is_guild:
            return

        key = self._scope_key(message)

        # ── Check for NAME quiz answer ────────────────────────────────────────
        active = self._active.get(key)
        if active and active.get("quiz_type") == QuizType.NAME:
            found = self._find_element_in_text(message.content)
            if found and found["name"].lower() == active["element"]["name"].lower():
                self._active.pop(key, None)
                self._cancel_timeout(key)
                self._reset_counter(key)
                new_total = await self._add_score(key, message.author.id)
                await message.channel.send(
                    embed=_build_win_embed(active["element"], message.author, new_total)
                )
                return

        # ── Check for incense NAME answer ─────────────────────────────────────
        incense_el = self._incense_active.get(message.channel.id)
        if incense_el:
            found = self._find_element_in_text(message.content)
            if found and found["name"].lower() == incense_el["name"].lower():
                self._incense_active.pop(message.channel.id, None)
                self._incense_last[message.channel.id] = incense_el["name"]
                new_total = await self._add_score(key, message.author.id)
                await message.channel.send(
                    embed=_build_win_embed(incense_el, message.author, new_total)
                )

        # ── Count toward next quiz trigger ────────────────────────────────────
        if is_guild:
            locked_channel = self._quiz_channel.get(message.guild.id)
            if locked_channel and message.channel.id != locked_channel:
                return

        count = self._increment(key)
        if count >= MESSAGES_PER_QUIZ and key not in self._active:
            self._reset_counter(key)
            await self._post_quiz(message.channel, key)

    # ── Incense ───────────────────────────────────────────────────────────────

    async def _incense_loop(
        self,
        channel: discord.TextChannel,
        scope_key: int | str,
        interval: int = INCENSE_INTERVAL,
    ) -> None:
        """Spawn a random quiz type every `interval` seconds until stopped."""
        try:
            while channel.id in self._incense_tasks:
                element    = random.choice(ELEMENTS)
                last       = self._incense_last.get(channel.id)
                quiz_type  = random.choice(list(QuizType))

                while last and element["name"] == last:
                    element = random.choice(ELEMENTS)

                embed = _build_incense_embed(element, quiz_type, last)

                if quiz_type == QuizType.NAME:
                    self._incense_active[channel.id] = element
                    await channel.send(embed=embed)
                    await asyncio.sleep(interval)

                    if channel.id in self._incense_active:
                        answered = self._incense_active.pop(channel.id, None)
                        if answered:
                            self._incense_last[channel.id] = answered["name"]
                            await channel.send(
                                f"⏰ Nobody got it! The element was **{answered['name']}** "
                                f"(`{answered['symbol']}` · #{answered['atomic_number']})"
                            )

                else:
                    # Button-type incense round
                    choices = _pick_choices(element)

                    answered = asyncio.Event()

                    async def on_correct(interaction: discord.Interaction, el: dict, _evt=answered):
                        self._incense_last[channel.id] = el["name"]
                        new_total = await self._add_score(scope_key, interaction.user.id)
                        await interaction.followup.send(
                            embed=_build_win_embed(el, interaction.user, new_total)
                        )
                        _evt.set()

                    async def on_timeout(_el=element, _evt=answered):
                        self._incense_last[channel.id] = _el["name"]
                        await channel.send(
                            f"⏰ Nobody got it! The element was **{_el['name']}** "
                            f"(`{_el['symbol']}` · #{_el['atomic_number']})"
                        )
                        _evt.set()

                    view = MultiChoiceView(
                        correct=element,
                        choices=choices,
                        on_correct=on_correct,
                        on_timeout=on_timeout,
                    )
                    await channel.send(embed=embed, view=view)
                    # Wait until answered or timed out before next round.
                    # TimeoutError here is normal (nobody answered) — just continue.
                    try:
                        await asyncio.wait_for(answered.wait(), timeout=interval + 5)
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            # Intentional stop (a!quiz incense stop or bot shutdown) — clean exit.
            self._incense_active.pop(channel.id, None)
        except Exception as exc:
            # Unexpected error (HTTP blip, Discord outage, etc.) — log and restart.
            print(f"[ElementQuiz] Incense loop crashed in {channel.id}: {exc!r} — restarting in 10s")
            self._incense_active.pop(channel.id, None)
            await asyncio.sleep(10)
            # Re-register and restart the loop so it survives transient errors.
            if channel.id not in self._incense_tasks:
                task = asyncio.get_event_loop().create_task(
                    self._incense_loop(channel, scope_key, interval=interval)
                )
                self._incense_tasks[channel.id] = task
                return  # this coroutine is done; the new task carries on
        finally:
            self._incense_tasks.pop(channel.id, None)
            self._incense_last.pop(channel.id, None)

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.group(name="quiz", invoke_without_command=True)
    async def quiz(self, ctx: commands.Context):
        """Element Quiz management commands."""
        await ctx.send_help(ctx.command)

    def _is_admin(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return True
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
            # Disable buttons if it was a button quiz
            msg = active.get("view_message")
            if msg:
                try:
                    await msg.edit(view=None)
                except discord.HTTPException:
                    pass
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
            qt = active.get("quiz_type", QuizType.NAME)
            lines.append(
                f"**Active quiz:** `{qt.value}` type · element masked as "
                f"`{_mask_name(active['element']['name'])}` in <#{active['channel_id']}>"
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
        """Lock quizzes to a specific channel. Usage: a!quiz setchannel #general"""
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
        """Remove the channel restriction."""
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
        """Show the element quiz leaderboard."""
        key  = self._scope_key(ctx.message)
        rows = await _db.quiz_get_scores(str(key), limit=LEADERBOARD_SIZE)
        scope_scores = {r["user_id"]: r["score"] for r in rows}
        scope_label  = f"Server: {ctx.guild.name}" if ctx.guild else "Your personal DM quiz session"
        embed = await _build_leaderboard_embed(scope_scores, self.bot, scope_label)
        await ctx.reply(embed=embed)

    # ── Incense commands ──────────────────────────────────────────────────────

    @quiz.group(name="incense", invoke_without_command=True)
    async def quiz_incense(self, ctx: commands.Context):
        """Start a rapid-fire incense quiz — random types every 20 seconds."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
            return
        if ctx.channel.id in self._incense_tasks:
            await ctx.reply("⚠️ Incense is already running here! Use `a!quiz incense stop` to end it.")
            return
        scope_key = self._scope_key(ctx.message)
        task = asyncio.get_event_loop().create_task(
            self._incense_loop(ctx.channel, scope_key)
        )
        self._incense_tasks[ctx.channel.id] = task
        await ctx.reply(
            "🧪 **Incense started!** Random quiz types will spawn every "
            f"{INCENSE_INTERVAL}s. Use `a!quiz incense stop` to end."
        )

    @quiz_incense.command(name="stop")
    async def quiz_incense_stop(self, ctx: commands.Context):
        """Stop the incense quiz running in this channel."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
            return
        task = self._incense_tasks.pop(ctx.channel.id, None)
        if task:
            task.cancel()
            await ctx.reply("🛑 Incense stopped.")
        else:
            await ctx.reply("ℹ️ No incense is running in this channel.")

    # ── Error handler ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if ctx.command is None or ctx.command.cog is not self:
            return
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(ElementQuizCog(bot))
