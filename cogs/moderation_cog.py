"""
cogs/moderation_cog.py
───────────────────────
Features
--------
• Message activity tracking — counts messages per user per hour (NOT content).
  Used for graphs (24h / 12h / 7d) and a 7-day leaderboard.
  Data auto-expires after 7 days via a MongoDB TTL index — no manual cleanup
  job needed; whatever is older than 7 days is simply gone.
• NSFW word filter — scans new messages AND edits. If a match is found, the
  bot waits NSFW_DELETE_DELAY seconds, then deletes the message. No warn/mute/
  ban commands, no logging — deletion is silent.

Requirements
------------
• "Message Content Intent" MUST be enabled in the Discord Developer Portal
  and in your bot's `intents` (intents.message_content = True), or the NSFW
  filter will not see any message text.
• matplotlib must be installed for /moderation graph to work.

Slash commands (all require Manage Guild)
-----------------------------------------
/moderation graph        user  period(24h/12h/7d)   → activity chart (image)
/moderation leaderboard  limit                        → top users, last 7 days
"""

import asyncio
import io
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    import matplotlib
    matplotlib.use("Agg")  # headless, no display needed
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    MPL_OK = True
except ImportError:
    MPL_OK = False
    print("[moderation_cog] matplotlib not installed — /moderation graph disabled.", file=sys.stderr)

import db as _db

def _col():
    return _db.get_db()

# ═══════════════════════════════════════════════════════════════════════════
# NSFW WORD LIST — edit freely. Case-insensitive, whole-word match (so
# "class" won't match "ass"). Add/remove words as needed; the regex below
# rebuilds automatically from this set.
# ═══════════════════════════════════════════════════════════════════════════
NSFW_WORDS_EN = {
    "porn", "pornography", "nsfw", "hentai", "xxx", "camgirl", "camwhore",
    "sext", "sexting", "cumshot", "blowjob", "handjob", "deepthroat",
    "anal", "boobs", "titties", "titjob", "nudes", "onlyfans", "creampie",
    "gangbang", "bukkake", "milf", "dildo", "vibrator", "masturbate",
    "masturbation", "orgasm", "jerkoff", "jackoff", "cumming", "rimjob",
    "fisting", "bondage", "bdsm", "fetish", "escort", "prostitute",
    "stripper", "webcam", "livecam", "fuck", "fucking", "fucked", "motherfucker",
    "shit", "bullshit", "bitch", "asshole", "dick", "cock", "pussy",
    "cunt", "slut", "whore", "twat", "wank", "nigger", "nigga", "faggot",
    "retard", "rape", "raping",
}

# Hindi profanity — romanized/Hinglish spellings (most Discord messages in
# Hindi are typed in Latin script) plus native Devanagari forms.
NSFW_WORDS_HI_ROMAN = {
    "chutiya", "chutiye", "chutiyapa", "madarchod", "mc", "bhenchod",
    "behenchod", "bc", "randi", "randi ka", "gandu", "gaand", "gand",
    "gandmasti", "lund", "lauda", "laude", "lauda lasan", "bhosda",
    "bhosdi", "bhosdike", "bhosdiwala", "chodu", "chod", "chodna",
    "harami", "haramzada", "haramzadi", "kutiya", "kutta", "kamina",
    "kamini", "saala", "saali", "raand", "suar", "gashti", "tatti",
    "chinal", "bhadwa", "bhadwe", "bur", "choot", "chut", "chutad",
}
NSFW_WORDS_HI_DEVANAGARI = {
    "चूतिया", "मादरचोद", "भेनचोद", "बहनचोद", "रंडी", "गांडू", "गांड",
    "लंड", "लौड़ा", "भोसड़ी", "भोसड़ीके", "चोद", "हरामी", "हरामज़ादा",
    "कुत्ता", "कुतिया", "कमीना", "साला", "साली", "सुअर", "चूत", "छिनाल",
}

NSFW_WORDS = NSFW_WORDS_EN | NSFW_WORDS_HI_ROMAN | NSFW_WORDS_HI_DEVANAGARI
# NOTE: this is a starter list, not exhaustive, in either language. Expand
# freely — just add/remove entries in the sets above; the regex rebuilds
# automatically from whatever NSFW_WORDS ends up containing.

NSFW_DELETE_DELAY = 7  # seconds to wait after (re)detecting a match before deleting


_DEVANAGARI_CHAR = re.compile(r"[\u0900-\u097F]")


def _build_nsfw_regex(words: set) -> "re.Pattern":
    """
    Latin-script words use standard \\b word boundaries.
    Devanagari words CANNOT use \\b — Python's \\w doesn't count vowel-sign
    combining marks (matras) as word characters, so \\b lands mid-word and
    silently fails to match. Instead, Devanagari words are bounded by
    "not another Devanagari character" via lookaround.
    """
    latin_words = [w for w in words if not _DEVANAGARI_CHAR.search(w)]
    deva_words  = [w for w in words if _DEVANAGARI_CHAR.search(w)]

    parts = []
    if latin_words:
        escaped = sorted((re.escape(w) for w in latin_words), key=len, reverse=True)
        parts.append(r"\b(?:" + "|".join(escaped) + r")\b")
    if deva_words:
        escaped = sorted((re.escape(w) for w in deva_words), key=len, reverse=True)
        parts.append(
            r"(?<![\u0900-\u097F])(?:" + "|".join(escaped) + r")(?![\u0900-\u097F])"
        )
    return re.compile("|".join(parts), re.IGNORECASE)


NSFW_REGEX = _build_nsfw_regex(NSFW_WORDS)

# ── Activity bucket settings ─────────────────────────────────────────────────
RETENTION_DAYS = 7
RETENTION_SECONDS = RETENTION_DAYS * 24 * 3600


# ── DB helpers ────────────────────────────────────────────────────────────────

def _hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


async def _track_message(guild_id: int, user_id: int) -> None:
    hour_start = _hour_floor(datetime.now(timezone.utc))
    await _col().mod_activity_hourly.update_one(
        {"guild_id": guild_id, "user_id": user_id, "hour_start": hour_start},
        {"$inc": {"count": 1}},
        upsert=True,
    )


async def _get_user_buckets(guild_id: int, user_id: int, since: datetime) -> list[dict]:
    cursor = _col().mod_activity_hourly.find(
        {"guild_id": guild_id, "user_id": user_id, "hour_start": {"$gte": since}}
    ).sort("hour_start", 1)
    return await cursor.to_list(length=None)


async def _get_leaderboard(guild_id: int, limit: int) -> list[dict]:
    pipeline = [
        {"$match": {"guild_id": guild_id}},
        {"$group": {"_id": "$user_id", "total": {"$sum": "$count"}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
    ]
    cursor = _col().mod_activity_hourly.aggregate(pipeline)
    return await cursor.to_list(length=None)


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_checks: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        # TTL index: any bucket doc auto-deletes RETENTION_SECONDS after its
        # hour_start. This alone satisfies "delete day 1's data once we hit
        # day 8" — Mongo handles it in the background, no cron needed.
        await _col().mod_activity_hourly.create_index(
            "hour_start", expireAfterSeconds=RETENTION_SECONDS
        )

    def cog_unload(self):
        for task in self._pending_checks.values():
            task.cancel()

    # ── Listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        await _track_message(message.guild.id, message.author.id)
        self._schedule_nsfw_check(message.id, message.channel.id)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or not after.guild:
            return
        if before.content == after.content:
            return  # e.g. embed-only edit, nothing new to scan
        self._schedule_nsfw_check(after.id, after.channel.id)

    # ── NSFW filter internals ───────────────────────────────────────────────

    def _schedule_nsfw_check(self, message_id: int, channel_id: int):
        existing = self._pending_checks.get(message_id)
        if existing and not existing.done():
            existing.cancel()  # a fresh edit resets the 7s clock
        task = asyncio.create_task(self._delayed_nsfw_check(message_id, channel_id))
        self._pending_checks[message_id] = task

    async def _delayed_nsfw_check(self, message_id: int, channel_id: int):
        try:
            await asyncio.sleep(NSFW_DELETE_DELAY)

            channel = self.bot.get_channel(channel_id)
            if channel is None:
                return
            try:
                msg = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden):
                return  # already deleted, or we lost access

            if NSFW_REGEX.search(msg.content):
                try:
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            self._pending_checks.pop(message_id, None)

    # ── Slash commands ────────────────────────────────────────────────────────

    grp = app_commands.Group(
        name="moderation",
        description="Message activity graphs and leaderboard",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @grp.command(name="graph", description="Show a message-activity graph for a user.")
    @app_commands.describe(user="User to graph (defaults to you)", period="Time range")
    @app_commands.choices(period=[
        app_commands.Choice(name="Last 24 hours", value="24h"),
        app_commands.Choice(name="Last 12 hours", value="12h"),
        app_commands.Choice(name="Last 7 days", value="7d"),
    ])
    async def graph(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str],
        user: Optional[discord.User] = None,
    ):
        if not MPL_OK:
            await interaction.response.send_message(
                "⚠️ matplotlib isn't installed on the bot host, so graphs are unavailable.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        target = user or interaction.user
        now = datetime.now(timezone.utc)

        if period.value == "24h":
            since = now - timedelta(hours=24)
        elif period.value == "12h":
            since = now - timedelta(hours=12)
        else:
            since = now - timedelta(days=7)

        buckets = await _get_user_buckets(interaction.guild_id, target.id, since)

        if not buckets:
            await interaction.followup.send(
                f"No tracked activity for **{target.display_name}** in that window."
            )
            return

        if period.value == "7d":
            # roll hourly buckets up into daily totals
            daily: dict[str, int] = {}
            for b in buckets:
                day_key = b["hour_start"].strftime("%Y-%m-%d")
                daily[day_key] = daily.get(day_key, 0) + b["count"]
            x = [datetime.strptime(k, "%Y-%m-%d") for k in daily.keys()]
            y = list(daily.values())
            date_fmt = mdates.DateFormatter("%b %d")
        else:
            x = [b["hour_start"] for b in buckets]
            y = [b["count"] for b in buckets]
            date_fmt = mdates.DateFormatter("%H:%M")

        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(x, y, width=0.03 if period.value != "7d" else 0.7, color="#5865F2")
        ax.set_title(f"{target.display_name} — messages ({period.name})")
        ax.set_ylabel("Messages")
        ax.xaxis.set_major_formatter(date_fmt)
        fig.autofmt_xdate()
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=140)
        plt.close(fig)
        buf.seek(0)

        total = sum(y)
        await interaction.followup.send(
            content=f"**{total}** messages from **{target.display_name}** ({period.name.lower()}):",
            file=discord.File(buf, filename="activity.png"),
        )

    @grp.command(name="leaderboard", description="Top users by message count (last 7 days).")
    @app_commands.describe(limit="How many users to show (default 10, max 25)")
    async def leaderboard(self, interaction: discord.Interaction, limit: Optional[int] = 10):
        limit = max(1, min(limit or 10, 25))
        await interaction.response.defer()

        rows = await _get_leaderboard(interaction.guild_id, limit)
        if not rows:
            await interaction.followup.send("No tracked activity yet.")
            return

        lines = []
        for i, row in enumerate(rows, start=1):
            member = interaction.guild.get_member(int(row["_id"]))
            name = member.display_name if member else f"`{row['_id']}`"
            lines.append(f"**{i}.** {name} — {row['total']} messages")

        embed = discord.Embed(
            title="📊 Message Leaderboard (last 7 days)",
            description="\n".join(lines),
            colour=0x5865F2,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))

