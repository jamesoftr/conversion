"""
cogs/element_quiz_cog.py  —  Periodic Table Element Quiz.

Quiz types (randomly chosen each round)
────────────────────────────────────────
  NAME   — masked element name + symbol + atomic# shown; type the answer in chat
  SYMBOL — element symbol shown; pick from 4 buttons
  ATOMIC — atomic number shown; pick from 4 buttons

All quiz types show a generated element card image.

Fonts
─────
  Downloaded on first import from https://github.com/cynthiaofpower/meowthfonts
  into  <bot_root>/fonts/   and cached there for all future runs.
  Fonts used: Poppins-Bold, Poppins-SemiBold, Poppins-Medium, Poppins-Regular.

Incense
───────
  Auto (startup)  — infinite spawns, INCENSE_AUTO_INTERVAL seconds each
  Manual          — INCENSE_MANUAL_SPAWNS spawns, INCENSE_INTERVAL seconds each

Commands  (Manage Guild)
────────────────────────
  a!quiz skip            — skip active quiz
  a!quiz status          — counter + active quiz info
  a!quiz trigger         — manually fire a quiz
  a!quiz setchannel [#]  — lock quizzes to a channel
  a!quiz clearchannel    — remove channel lock
  a!quiz scores          — leaderboard
  a!quiz incense start   — start INCENSE_MANUAL_SPAWNS-spawn manual incense
  a!quiz incense stop    — stop incense
  a!quiz hint            — first letter + length clue for active NAME quiz
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import re
import time
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from elements import ELEMENTS, get_by_name
import db as _db

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

MESSAGES_PER_QUIZ      = 10
POKETWO_ID             = 716390085896962058
QUIZ_TIMEOUT_SECONDS   = 120
LEADERBOARD_SIZE       = 10
WRONG_COOLDOWN         = 5        # seconds penalty for wrong button click
HINT_COOLDOWN          = 30       # seconds between hint uses per user

INCENSE_INTERVAL       = 20      # seconds between spawns for manual incense
INCENSE_MANUAL_SPAWNS  = 30      # number of spawns for a!quiz incense start
INCENSE_AUTO_INTERVAL  = 20      # seconds between spawns for auto-startup incense

# Set to a channel ID to auto-start infinite incense on startup, or None to disable.
INCENSE_AUTO_CHANNEL_ID: Optional[int] = 1506869977636933743


# ─────────────────────────────────────────────────────────────────────────────
# Font bootstrap  (downloads Poppins from GitHub on first run, then caches)
# ─────────────────────────────────────────────────────────────────────────────

_FONT_DIR  = Path(__file__).parent / "fonts"
_FONT_BASE = "https://raw.githubusercontent.com/cynthiaofpower/meowthfonts/main/fonts"
_FONT_FILES = [
    "Poppins-Bold.ttf",
    "Poppins-SemiBold.ttf",
    "Poppins-Medium.ttf",
    "Poppins-MediumItalic.ttf",
    "Poppins-Regular.ttf",
]


def _ensure_fonts() -> None:
    """
    Download any missing Poppins font files from GitHub into <cog_dir>/fonts/.
    Called once at module import; safe to call again (no-ops if all present).
    Logs a warning and falls back to PIL's built-in font if a download fails.
    """
    _FONT_DIR.mkdir(parents=True, exist_ok=True)
    for fname in _FONT_FILES:
        dest = _FONT_DIR / fname
        if dest.exists():
            continue
        url = f"{_FONT_BASE}/{fname}"
        try:
            log.info("[ElementQuiz] Downloading font %s …", fname)
            urllib.request.urlretrieve(url, dest)
            log.info("[ElementQuiz] Saved %s", dest)
        except Exception as exc:
            log.warning("[ElementQuiz] Could not download %s: %r", fname, exc)


# Run immediately on import so fonts are ready before the first image is drawn.
_ensure_fonts()


def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a Poppins variant by filename; fall back to PIL default on failure."""
    try:
        return ImageFont.truetype(str(_FONT_DIR / name), size)
    except Exception:
        return ImageFont.load_default()


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────────────────────────────────────

class QuizType(Enum):
    NAME   = "name"
    SYMBOL = "symbol"
    ATOMIC = "atomic"


_CATEGORY_COLORS: dict[str, tuple[int, int, int]] = {
    "nonmetal":              (31,  119, 180),
    "noble gas":             (148, 103, 189),
    "alkali metal":          (214,  39,  40),
    "alkaline earth metal":  (255, 127,  14),
    "metalloid":             (44,  160,  44),
    "halogen":               (23,  190, 207),
    "transition metal":      (140,  86,  75),
    "post-transition metal": (188, 189,  34),
    "lanthanide":            (227, 119, 194),
    "actinide":              (127, 127, 127),
}
_DEFAULT_COLOR = (70, 130, 180)

WHITE       = (255, 255, 255)
WHITE_DIM   = (255, 255, 255, 180)
WHITE_FAINT = (255, 255, 255, 60)


def _element_color(element: dict) -> discord.Color:
    cat = element.get("category", "").lower()
    rgb = next((v for k, v in _CATEGORY_COLORS.items() if k in cat), _DEFAULT_COLOR)
    return discord.Color.from_rgb(*rgb)


# ─────────────────────────────────────────────────────────────────────────────
# Image generation
# ─────────────────────────────────────────────────────────────────────────────
#
#  Card is 200 × 200 px  (small enough for Discord embeds, sharp on mobile).
#  Layout (portrait tile):
#
#   ┌─────────────────────┐
#   │ 79          Transition│  ← atomic# (SemiBold 18) | category (Regular 10)
#   │                      │
#   │          Au          │  ← symbol centred (Bold 72) — or "?" when hidden
#   │                      │
#   │          Gold        │  ← name centred (SemiBold 16)
#   │                      │
#   │   H _ _ _ _ _ _ _    │  ← masked hint row (Medium 13) — NAME quiz only
#   └─────────────────────┘
#
#  When revealed=True: symbol + name shown, hint row hidden.
#  When revealed=False:
#      NAME quiz  → "?" centre + masked hint row at bottom
#      SYMBOL quiz→ "?" centre only (symbol is the clue in the embed)
#      ATOMIC quiz→ "?" centre only

_W, _H = 200, 200


def _make_element_image(
    element:   dict,
    revealed:  bool = False,
    quiz_type: Optional[QuizType] = None,
    hint_mask: Optional[str] = None,   # pre-computed _mask_name() string
) -> discord.File:
    """
    Generate a 200×200 element tile PNG.

    revealed=False + quiz_type=NAME  → '?' + masked name hint row at bottom
    revealed=False + other/None      → '?' only
    revealed=True                    → symbol + full name
    """
    cat   = element.get("category", "").lower()
    color = next((v for k, v in _CATEGORY_COLORS.items() if k in cat), _DEFAULT_COLOR)
    dark  = tuple(max(0, c - 55) for c in color)
    mid   = tuple(max(0, c - 25) for c in color)

    img  = Image.new("RGB", (_W, _H), color)
    draw = ImageDraw.Draw(img, "RGBA")

    # Subtle gradient-ish bottom strip
    for y in range(_H - 40, _H):
        alpha = int(60 * (y - (_H - 40)) / 40)
        draw.line([(0, y), (_W, y)], fill=(*dark, alpha))

    # Outer border
    draw.rectangle([0, 0, _W - 1, _H - 1], outline=dark, width=5)
    # Inner border
    draw.rectangle([7, 7, _W - 8, _H - 8], outline=(*WHITE, 40), width=1)

    # ── Top row: atomic number (left) + category (right) ─────────────────────
    fn_num  = _load_font("Poppins-SemiBold.ttf", 18)
    fn_cat  = _load_font("Poppins-Regular.ttf",  10)

    draw.text((13, 10), str(element["atomic_number"]), font=fn_num, fill=WHITE)

    cat_label = cat.title() if cat else ""
    # Right-align category; anchor="ra" = right-baseline
    draw.text((_W - 11, 10), cat_label, font=fn_cat, fill=(*WHITE, 180), anchor="ra")

    # ── Centre: symbol or "?" ─────────────────────────────────────────────────
    if revealed:
        fn_sym  = _load_font("Poppins-Bold.ttf",    72)
        fn_name = _load_font("Poppins-SemiBold.ttf", 16)
        # Symbol at vertical centre, nudged up slightly to make room for name
        draw.text((_W // 2, _H // 2 - 10), element["symbol"],
                  font=fn_sym, fill=WHITE, anchor="mm")
        # Name below symbol
        draw.text((_W // 2, _H // 2 + 52), element["name"],
                  font=fn_name, fill=(*WHITE, 220), anchor="mm")
    else:
        fn_q = _load_font("Poppins-Bold.ttf", 90)
        draw.text((_W // 2, _H // 2 - 8), "?", font=fn_q, fill=(*WHITE, 210), anchor="mm")

        # ── Hint row for NAME quiz ────────────────────────────────────────────
        if quiz_type == QuizType.NAME:
            mask = hint_mask or _mask_name(element["name"])
            fn_hint = _load_font("Poppins-Medium.ttf", 13)
            # Pill background
            tw = draw.textlength(mask, font=fn_hint)
            px, py = (_W - tw) // 2, _H - 28
            draw.rounded_rectangle(
                [px - 8, py - 4, px + tw + 8, py + 18],
                radius=6,
                fill=(*dark, 160),
            )
            draw.text((px, py), mask, font=fn_hint, fill=WHITE)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="element.png")


# ─────────────────────────────────────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mask_name(name: str) -> str:
    """Every other letter → '＿'. Spaces/hyphens kept."""
    result, idx = [], 0
    for ch in name:
        if ch.isalpha():
            result.append(ch.upper() if idx % 2 == 0 else "＿")
            idx += 1
        else:
            result.append(ch)
    return "".join(result)


def _pick_choices(correct: dict) -> list[dict]:
    """Return the correct element + 3 random wrong elements, shuffled."""
    pool    = [e for e in ELEMENTS if e["name"] != correct["name"]]
    choices = random.sample(pool, 3) + [correct]
    random.shuffle(choices)
    return choices


# ─────────────────────────────────────────────────────────────────────────────
# Embeds
# ─────────────────────────────────────────────────────────────────────────────

def _quiz_embed(
    element:    dict,
    quiz_type:  QuizType,
    *,
    incense:    bool  = False,
    last_name:  Optional[str] = None,
    spawns_remaining: Optional[int] = None,
    spawn_interval:   int = INCENSE_INTERVAL,
) -> discord.Embed:
    color = _element_color(element)

    if quiz_type == QuizType.NAME:
        clue  = (
            f"**Symbol:** `{element['symbol']}`  ·  "
            f"**Atomic #:** `{element['atomic_number']}`\n\n"
            f"*Type the element name in chat!*"
        )
        title = "🧪 A wild element appeared!" if incense else "🔬 Guess the Element!"

    elif quiz_type == QuizType.SYMBOL:
        clue  = (
            f"**Which element has the symbol `{element['symbol']}`?**\n\n"
            f"*Click the correct button!*"
        )
        title = "🧪 A wild element appeared!" if incense else "🔣 Guess the Element!"

    else:  # ATOMIC
        clue  = (
            f"**Which element has atomic number `{element['atomic_number']}`?**\n\n"
            f"*Click the correct button!*"
        )
        title = "🧪 A wild element appeared!" if incense else "🔢 Guess the Element!"

    if incense and last_name:
        description = f"Last element: **{last_name}**\n\n{clue}"
    else:
        description = clue

    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_image(url="attachment://element.png")

    if incense:
        remaining_str = "∞" if spawns_remaining is None else str(spawns_remaining)
        embed.set_footer(text=(
            f"Incense: Active  ·  Spawns left: {remaining_str}  ·  Interval: {spawn_interval}s"
        ))
    else:
        if quiz_type == QuizType.NAME:
            embed.set_footer(
                text=f"Type the name in chat  ·  {QUIZ_TIMEOUT_SECONDS}s  ·  a!quiz hint for a clue"
            )
        else:
            embed.set_footer(
                text=f"Click a button  ·  {QUIZ_TIMEOUT_SECONDS}s  ·  Wrong = {WRONG_COOLDOWN}s cooldown"
            )
    return embed


def _win_embed(element: dict, winner: discord.User | discord.Member, new_total: int) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Correct!",
        description=(
            f"🎉 **{winner.display_name}** got it!\n\n"
            f"The element was **{element['name']}**\n"
            f"`{element['symbol']}` · Atomic #{element['atomic_number']}\n\n"
            f"**Total correct:** `{new_total}` 🏆"
        ),
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=winner.display_avatar.url)
    embed.set_image(url="attachment://element.png")
    return embed


def _timeout_embed(element: dict) -> discord.Embed:
    embed = discord.Embed(
        title="⏰ Nobody got it!",
        description=(
            f"The element was **{element['name']}**\n"
            f"`{element['symbol']}` · Atomic #{element['atomic_number']}"
        ),
        color=discord.Color.red(),
    )
    embed.set_image(url="attachment://element.png")
    embed.set_footer(text="Better luck next time!")
    return embed


async def _build_leaderboard_embed(
    scores: dict[int, int],
    bot: commands.Bot,
    scope_label: str,
) -> discord.Embed:
    if not scores:
        e = discord.Embed(
            title="🏆 Element Quiz Leaderboard",
            description="No scores yet — be the first!",
            color=discord.Color.gold(),
        )
        e.set_footer(text=scope_label)
        return e

    lines  = []
    medals = ["🥇", "🥈", "🥉"]
    for rank, (uid, score) in enumerate(
        sorted(scores.items(), key=lambda x: x[1], reverse=True)[:LEADERBOARD_SIZE],
        start=1,
    ):
        user = bot.get_user(int(uid))
        if user is None:
            try:
                user = await bot.fetch_user(int(uid))
            except discord.NotFound:
                user = None
        name  = user.display_name if user else "Unknown"
        medal = medals[rank - 1] if rank <= 3 else f"`#{rank}`"
        lines.append(f"{medal} **{name}** <@{uid}> — `{score}` correct")

    e = discord.Embed(
        title="🏆 Element Quiz Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    e.set_footer(text=scope_label)
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Button view
# ─────────────────────────────────────────────────────────────────────────────

class MultiChoiceView(discord.ui.View):
    """4 buttons for SYMBOL / ATOMIC quiz types."""

    def __init__(
        self,
        correct:    dict,
        choices:    list[dict],
        on_correct,     # async (interaction, element) → None
        on_timeout,     # async () → None
    ):
        super().__init__(timeout=QUIZ_TIMEOUT_SECONDS)
        self._correct         = correct
        self._on_correct      = on_correct
        self._on_timeout      = on_timeout
        self._cooldowns:      dict[int, float] = {}
        self.resolved         = False
        self._force_disabled  = False

        for choice in choices:
            btn           = discord.ui.Button(
                label     = choice["name"],
                style     = discord.ButtonStyle.primary,
                custom_id = choice["name"],
            )
            btn.callback  = self._make_cb(choice)
            self.add_item(btn)

    def _make_cb(self, choice: dict):
        async def cb(interaction: discord.Interaction):
            if self._force_disabled or self.resolved:
                await interaction.response.send_message("⚡ This quiz already ended!", ephemeral=True)
                return
            now   = time.monotonic()
            until = self._cooldowns.get(interaction.user.id, 0)
            if now < until:
                await interaction.response.send_message(
                    f"⏳ Cooldown! Try again in **{round(until - now, 1)}s**.", ephemeral=True
                )
                return
            if choice["name"] == self._correct["name"]:
                self.resolved = True
                self._colour()
                await interaction.response.edit_message(view=self)
                await self._on_correct(interaction, self._correct)
            else:
                self._cooldowns[interaction.user.id] = now + WRONG_COOLDOWN
                await interaction.response.send_message(
                    f"❌ **{choice['name']}** is wrong! Wait **{WRONG_COOLDOWN}s**.", ephemeral=True
                )
        return cb

    def _colour(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.style    = (
                    discord.ButtonStyle.success
                    if item.custom_id == self._correct["name"]
                    else discord.ButtonStyle.danger
                )
                item.disabled = True

    def force_disable(self):
        self._force_disabled = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        self.stop()

    async def on_timeout(self):
        if self.resolved or self._force_disabled:
            return
        self.resolved = True
        self._colour()
        await self._on_timeout()


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class ElementQuizCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._counters:         dict[int | str, int]         = {}
        self._active:           dict[int | str, dict]        = {}
        self._quiz_channel:     dict[int, int | None]        = {}
        self._timeout_tasks:    dict[int | str, asyncio.Task] = {}

        self._incense_tasks:    dict[int, asyncio.Task]      = {}
        self._incense_last:     dict[int, str]               = {}
        self._incense_active:   dict[int, dict]              = {}
        self._incense_answered: dict[int, asyncio.Event]     = {}

        # Tracks the last button-quiz message per channel so we can always
        # disable its buttons when the next element spawns — regardless of
        # whether that round was answered, timed out, or skipped.
        self._last_button_msg: dict[int, tuple[discord.Message, MultiChoiceView]] = {}

        self._hint_cooldowns:   dict[int, float]             = {}

        self._sorted_names: list[str] = sorted(
            (e["name"].lower() for e in ELEMENTS), key=len, reverse=True
        )

    # ── Startup / teardown ────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        if not INCENSE_AUTO_CHANNEL_ID:
            return

        async def _start():
            await self.bot.wait_until_ready()
            ch = self.bot.get_channel(INCENSE_AUTO_CHANNEL_ID)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(INCENSE_AUTO_CHANNEL_ID)
                except discord.HTTPException:
                    log.warning("[ElementQuiz] Auto-incense: channel %s not found", INCENSE_AUTO_CHANNEL_ID)
                    return
            if ch.id in self._incense_tasks:
                return
            scope = ch.guild.id if getattr(ch, "guild", None) else f"dm_{INCENSE_AUTO_CHANNEL_ID}"
            self._incense_tasks[ch.id] = asyncio.get_running_loop().create_task(
                self._incense_loop(ch, scope, interval=INCENSE_AUTO_INTERVAL, max_spawns=None)
            )
            log.info("[ElementQuiz] Auto-incense started in #%s (%s)", ch.name, ch.id)

        asyncio.get_running_loop().create_task(_start())

    async def cog_unload(self) -> None:
        for task in list(self._incense_tasks.values()):
            task.cancel()
        for task in list(self._timeout_tasks.values()):
            task.cancel()
        self._incense_tasks.clear()
        self._timeout_tasks.clear()
        self._incense_active.clear()
        self._incense_answered.clear()
        self._last_button_msg.clear()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _scope_key(self, msg: discord.Message) -> int | str:
        return msg.guild.id if msg.guild else f"dm_{msg.author.id}"

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

    async def _disable_last_buttons(self, channel_id: int) -> None:
        """
        Disable and grey-out the buttons on the most recent button-quiz message
        in this channel, then forget it.  Called unconditionally before every
        new element spawn — covers answered, timed-out, and skipped rounds alike.
        """
        entry = self._last_button_msg.pop(channel_id, None)
        if entry is None:
            return
        msg, view = entry
        if not view._force_disabled:
            view.force_disable()
        try:
            await msg.edit(view=view)
        except discord.HTTPException:
            pass

    def _find_element(self, text: str) -> Optional[dict]:
        t = text.lower()
        for name in self._sorted_names:
            if re.search(rf"\b{re.escape(name)}\b", t):
                return get_by_name(name)
        return None

    # ── Regular quiz ──────────────────────────────────────────────────────────

    async def _quiz_timeout_task(
        self, key: int | str, channel: discord.TextChannel | discord.DMChannel
    ) -> None:
        await asyncio.sleep(QUIZ_TIMEOUT_SECONDS)
        active = self._active.pop(key, None)
        self._timeout_tasks.pop(key, None)
        if active:
            self._reset_counter(key)
            try:
                el   = active["element"]
                file = _make_element_image(el, revealed=True)
                await channel.send(embed=_timeout_embed(el), file=file)
            except discord.HTTPException:
                pass

    async def _post_quiz(
        self,
        channel:   discord.TextChannel | discord.DMChannel,
        key:       int | str,
        quiz_type: Optional[QuizType] = None,
    ) -> None:
        element   = random.choice(ELEMENTS)
        quiz_type = quiz_type or random.choice(list(QuizType))

        self._active[key] = {
            "element":    element,
            "channel_id": channel.id,
            "quiz_type":  quiz_type,
            "view":       None,
            "view_msg":   None,
        }
        self._cancel_timeout(key)

        # Disable any leftover buttons from the previous round in this channel
        await self._disable_last_buttons(channel.id)

        mask  = _mask_name(element["name"]) if quiz_type == QuizType.NAME else None
        file  = _make_element_image(element, revealed=False, quiz_type=quiz_type, hint_mask=mask)
        embed = _quiz_embed(element, quiz_type)

        if quiz_type == QuizType.NAME:
            await channel.send(embed=embed, file=file)
            self._timeout_tasks[key] = asyncio.get_running_loop().create_task(
                self._quiz_timeout_task(key, channel)
            )
        else:
            choices = _pick_choices(element)

            async def on_correct(interaction: discord.Interaction, el: dict):
                self._active.pop(key, None)
                self._cancel_timeout(key)
                self._reset_counter(key)
                total = await self._add_score(key, interaction.user.id)
                f2    = _make_element_image(el, revealed=True)
                await interaction.followup.send(embed=_win_embed(el, interaction.user, total), file=f2)

            async def on_timeout():
                act = self._active.pop(key, None)
                self._cancel_timeout(key)
                if act:
                    self._reset_counter(key)
                    try:
                        el = act["element"]
                        f2 = _make_element_image(el, revealed=True)
                        await channel.send(embed=_timeout_embed(el), file=f2)
                    except discord.HTTPException:
                        pass

            view = MultiChoiceView(element, choices, on_correct, on_timeout)
            msg  = await channel.send(embed=embed, file=file, view=view)
            self._active[key]["view"]     = view
            self._active[key]["view_msg"] = msg
            self._last_button_msg[channel.id] = (msg, view)

    # ── Incense loop ──────────────────────────────────────────────────────────

    async def _incense_loop(
        self,
        channel:    discord.TextChannel,
        scope_key:  int | str,
        interval:   int  = INCENSE_INTERVAL,
        max_spawns: Optional[int] = INCENSE_MANUAL_SPAWNS,
    ) -> None:
        spawned:           int                       = 0
        retry_after_error: bool                      = False

        while channel.id in self._incense_tasks:
            if max_spawns is not None and spawned >= max_spawns:
                self._incense_tasks.pop(channel.id, None)
                self._incense_last.pop(channel.id, None)
                try:
                    await channel.send(f"🛑 Incense session ended — all {INCENSE_MANUAL_SPAWNS} spawns used!")
                except discord.HTTPException:
                    pass
                return

            if retry_after_error:
                await asyncio.sleep(10)
                retry_after_error = False
                if channel.id not in self._incense_tasks:
                    return

            try:
                # Disable buttons from the previous round before posting the next one
                await self._disable_last_buttons(channel.id)

                last_name = self._incense_last.get(channel.id)
                element   = random.choice(ELEMENTS)
                while last_name and element["name"] == last_name:
                    element = random.choice(ELEMENTS)

                quiz_type   = random.choice(list(QuizType))
                spawns_left = None if max_spawns is None else (max_spawns - spawned)

                mask  = _mask_name(element["name"]) if quiz_type == QuizType.NAME else None
                file  = _make_element_image(element, revealed=False, quiz_type=quiz_type, hint_mask=mask)
                embed = _quiz_embed(
                    element, quiz_type,
                    incense=True,
                    last_name=last_name,
                    spawns_remaining=spawns_left,
                    spawn_interval=interval,
                )

                answered = asyncio.Event()

                if quiz_type == QuizType.NAME:
                    self._incense_active[channel.id]   = element
                    self._incense_answered[channel.id] = answered
                    await channel.send(embed=embed, file=file)

                    try:
                        await asyncio.wait_for(answered.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        self._incense_answered.pop(channel.id, None)

                    if channel.id in self._incense_active:
                        unanswered = self._incense_active.pop(channel.id, None)
                        if unanswered:
                            self._incense_last[channel.id] = unanswered["name"]

                else:
                    async def on_correct(
                        interaction: discord.Interaction,
                        el: dict,
                        _evt=answered,
                        _sk=scope_key,
                    ):
                        self._incense_last[channel.id] = el["name"]
                        total = await self._add_score(_sk, interaction.user.id)
                        f2    = _make_element_image(el, revealed=True)
                        await interaction.followup.send(
                            embed=_win_embed(el, interaction.user, total), file=f2
                        )
                        _evt.set()

                    async def on_timeout(_el=element, _evt=answered):
                        self._incense_last[channel.id] = _el["name"]
                        _evt.set()

                    view = MultiChoiceView(element, _pick_choices(element), on_correct, on_timeout)
                    msg  = await channel.send(embed=embed, file=file, view=view)
                    self._last_button_msg[channel.id] = (msg, view)

                    try:
                        await asyncio.wait_for(answered.wait(), timeout=interval + QUIZ_TIMEOUT_SECONDS + 5)
                    except asyncio.TimeoutError:
                        self._incense_last[channel.id] = element["name"]

                spawned += 1
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                self._incense_active.pop(channel.id, None)
                self._incense_answered.pop(channel.id, None)
                self._incense_tasks.pop(channel.id, None)
                self._incense_last.pop(channel.id, None)
                self._last_button_msg.pop(channel.id, None)
                return

            except Exception as exc:
                log.error("[ElementQuiz] Incense error in %s: %r — retrying in 10s", channel.id, exc)
                self._incense_active.pop(channel.id, None)
                self._incense_answered.pop(channel.id, None)
                retry_after_error = True

    # ── Message listener ──────────────────────────────────────────────────────

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

        # ── Regular NAME quiz answer ──────────────────────────────────────────
        active = self._active.get(key)
        if active and active.get("quiz_type") == QuizType.NAME:
            found = self._find_element(message.content)
            if found and found["name"].lower() == active["element"]["name"].lower():
                self._active.pop(key, None)
                self._cancel_timeout(key)
                self._reset_counter(key)
                total = await self._add_score(key, message.author.id)
                f2    = _make_element_image(found, revealed=True)
                await message.channel.send(
                    embed=_win_embed(found, message.author, total), file=f2
                )
                return

        # ── Incense NAME quiz answer ──────────────────────────────────────────
        inc_el = self._incense_active.get(message.channel.id)
        if inc_el:
            found = self._find_element(message.content)
            if found and found["name"].lower() == inc_el["name"].lower():
                self._incense_active.pop(message.channel.id, None)
                self._incense_last[message.channel.id] = inc_el["name"]
                total = await self._add_score(key, message.author.id)
                f2    = _make_element_image(found, revealed=True)
                await message.channel.send(
                    embed=_win_embed(found, message.author, total), file=f2
                )
                evt = self._incense_answered.get(message.channel.id)
                if evt:
                    evt.set()

        # ── Message counter → regular quiz ────────────────────────────────────
        if is_guild:
            locked = self._quiz_channel.get(message.guild.id)
            if locked and message.channel.id != locked:
                return

        count = self._increment(key)
        if count >= MESSAGES_PER_QUIZ and key not in self._active:
            self._reset_counter(key)
            await self._post_quiz(message.channel, key)

    # ── Commands ──────────────────────────────────────────────────────────────

    def _is_admin(self, ctx: commands.Context) -> bool:
        return ctx.guild is None or ctx.author.guild_permissions.manage_guild

    @commands.group(name="quiz", invoke_without_command=True)
    async def quiz(self, ctx: commands.Context):
        """Element Quiz commands."""
        await ctx.send_help(ctx.command)

    @quiz.command(name="skip")
    async def quiz_skip(self, ctx: commands.Context):
        """Skip the active quiz."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission.")
            return
        key    = self._scope_key(ctx.message)
        self._cancel_timeout(key)
        active = self._active.pop(key, None)
        if active:
            await self._disable_last_buttons(active["channel_id"])
            await ctx.reply(f"⏭️ Skipped. The element was **{active['element']['name']}**.")
        else:
            await ctx.reply("ℹ️ No active quiz right now.")

    @quiz.command(name="status")
    async def quiz_status(self, ctx: commands.Context):
        """Show counter and active quiz state."""
        key    = self._scope_key(ctx.message)
        count  = self._counters.get(key, 0)
        active = self._active.get(key)
        lines  = [
            f"**Messages until next quiz:** `{MESSAGES_PER_QUIZ - count}` "
            f"*(counter: {count}/{MESSAGES_PER_QUIZ})*",
        ]
        if active:
            qt = active.get("quiz_type", QuizType.NAME)
            lines.append(f"**Active quiz:** `{qt.value}` type in <#{active['channel_id']}>")
        else:
            lines.append("**Active quiz:** None")
        if ctx.guild:
            locked = self._quiz_channel.get(ctx.guild.id)
            lines.append(f"**Quiz channel:** <#{locked}>" if locked else "**Quiz channel:** Any")
            inc_running = ctx.channel.id in self._incense_tasks
            lines.append(f"**Incense:** {'🔥 Running' if inc_running else 'Off'}")
        else:
            lines.append("**Scope:** DM session")
        await ctx.reply(embed=discord.Embed(
            title="🔬 Element Quiz Status",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        ))

    @quiz.command(name="trigger")
    async def quiz_trigger(self, ctx: commands.Context):
        """Manually fire a quiz now."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission.")
            return
        key = self._scope_key(ctx.message)
        if key in self._active:
            await ctx.reply("⚠️ A quiz is already active! Use `a!quiz skip` first.")
            return
        self._reset_counter(key)
        await self._post_quiz(ctx.channel, key)

    @quiz.command(name="setchannel")
    async def quiz_setchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Lock quizzes to a channel. Usage: a!quiz setchannel #channel"""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission.")
            return
        if not ctx.guild:
            await ctx.reply("ℹ️ Only works in servers.")
            return
        target = channel or ctx.channel
        self._quiz_channel[ctx.guild.id] = target.id
        await ctx.reply(f"✅ Quizzes locked to {target.mention}.")

    @quiz.command(name="clearchannel")
    async def quiz_clearchannel(self, ctx: commands.Context):
        """Remove the channel lock."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission.")
            return
        if not ctx.guild:
            await ctx.reply("ℹ️ Only works in servers.")
            return
        self._quiz_channel.pop(ctx.guild.id, None)
        await ctx.reply("✅ Channel restriction removed.")

    @quiz.command(name="scores", aliases=["leaderboard"])
    async def quiz_scores(self, ctx: commands.Context):
        """Show the leaderboard."""
        key   = self._scope_key(ctx.message)
        rows  = await _db.quiz_get_scores(str(key), limit=LEADERBOARD_SIZE)
        embed = await _build_leaderboard_embed(
            {r["user_id"]: r["score"] for r in rows},
            self.bot,
            f"Server: {ctx.guild.name}" if ctx.guild else "DM session",
        )
        await ctx.reply(embed=embed)

    @quiz.command(name="hint")
    async def quiz_hint(self, ctx: commands.Context):
        """Get a hint for the active NAME quiz (first letter + letter count)."""
        key    = self._scope_key(ctx.message)
        active = self._active.get(key)
        if not active or active.get("quiz_type") != QuizType.NAME:
            await ctx.reply("ℹ️ No active name quiz right now.")
            return
        now   = time.monotonic()
        until = self._hint_cooldowns.get(ctx.author.id, 0)
        if now < until:
            await ctx.reply(f"⏳ Hint cooldown! Try again in **{round(until - now, 1)}s**.")
            return
        self._hint_cooldowns[ctx.author.id] = now + HINT_COOLDOWN
        name = active["element"]["name"]
        await ctx.reply(f"💡 Hint: starts with **{name[0].upper()}** — {len(name)} letters")

    # ── Incense commands ──────────────────────────────────────────────────────

    @quiz.group(name="incense", invoke_without_command=True)
    async def quiz_incense(self, ctx: commands.Context):
        """Incense quiz commands. Use `a!quiz incense start` / `stop`."""
        await ctx.send_help(ctx.command)

    @quiz_incense.command(name="start")
    async def quiz_incense_start(self, ctx: commands.Context):
        """Start a manual incense quiz session."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission.")
            return
        if not ctx.guild:
            await ctx.reply("ℹ️ Incense only works in servers.")
            return
        if ctx.channel.id in self._incense_tasks:
            await ctx.reply("⚠️ Incense is already running! Use `a!quiz incense stop` to end it.")
            return
        scope = self._scope_key(ctx.message)
        self._incense_tasks[ctx.channel.id] = asyncio.get_running_loop().create_task(
            self._incense_loop(
                ctx.channel, scope,
                interval=INCENSE_INTERVAL,
                max_spawns=INCENSE_MANUAL_SPAWNS,
            )
        )
        await ctx.reply(
            f"🧪 **Incense started!** {INCENSE_MANUAL_SPAWNS} spawns · "
            f"{INCENSE_INTERVAL}s interval · Use `a!quiz incense stop` to end early."
        )

    @quiz_incense.command(name="stop")
    async def quiz_incense_stop(self, ctx: commands.Context):
        """Stop the running incense session."""
        if not self._is_admin(ctx):
            await ctx.reply("❌ You need **Manage Guild** permission.")
            return
        task = self._incense_tasks.pop(ctx.channel.id, None)
        if task:
            task.cancel()
            await ctx.reply("🛑 Incense stopped.")
        else:
            await ctx.reply("ℹ️ No incense running in this channel.")

    # ── Error handler ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if ctx.command is None or ctx.command.cog is not self:
            return
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            await ctx.reply("❌ You need **Manage Guild** permission.")
        elif isinstance(error, commands.CommandNotFound):
            pass
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(f"⚠️ Bad argument: {error}")
        else:
            log.exception("[ElementQuiz] Unhandled command error in %s", ctx.command, exc_info=error)
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(ElementQuizCog(bot))
