"""
cogs/repeat_cog.py
──────────────────
Repeating Messages — auto-resume on bot restart.

Features
--------
• One repeating message per channel (enforced).
• Set interval (seconds / minutes / hours / days) and repeat count (or infinite).
• All configs stored in MongoDB (`repeat_messages` collection).
• On startup, every active entry is resumed automatically.
• Pagination on status views (server + global).

Slash commands  (group: /repeat)
---------------------------------
/repeat set      channel interval unit count message   — create / replace
/repeat remove   channel                               — stop & delete
/repeat pause    channel                               — pause without deleting
/repeat resume   channel                               — resume a paused entry
/repeat status                                         — this guild's entries (paged)
/repeat globalstatus                                   — all guilds (bot-owner only, paged)

Prefix commands  (group: a!repeat)
-----------------------------------
a!repeat set   #channel <interval> <unit> <count|inf> <message…>
a!repeat remove #channel
a!repeat pause  #channel
a!repeat resume #channel
a!repeat status
a!repeat globalstatus   (bot-owner only)

Interval units: s / sec / seconds  |  m / min / minutes
              | h / hr / hours     |  d / day / days
Count: positive integer for fixed repeats, 0 or "inf" / "infinite" for forever.

Minimum interval: 30 seconds.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db as _db

# ── helpers ───────────────────────────────────────────────────────────────────

PAGE_SIZE = 5   # entries per status page

UNIT_MULTIPLIERS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
MIN_INTERVAL = 30   # seconds


def _col():
    return _db.get_db().repeat_messages


def _parse_interval(value: str, unit: str) -> int | None:
    """Return interval in seconds, or None on bad input."""
    try:
        v = float(value)
    except ValueError:
        return None
    mult = UNIT_MULTIPLIERS.get(unit.lower())
    if mult is None:
        return None
    secs = int(v * mult)
    return secs if secs >= MIN_INTERVAL else None


def _parse_count(raw: str) -> int | None:
    """Return repeat count (0 = infinite), or None on bad input."""
    if raw.lower() in ("inf", "infinite", "forever", "0"):
        return 0
    try:
        n = int(raw)
        return n if n > 0 else None
    except ValueError:
        return None


def _fmt_interval(secs: int) -> str:
    if secs % 86400 == 0:
        v = secs // 86400
        return f"{v} day{'s' if v != 1 else ''}"
    if secs % 3600 == 0:
        v = secs // 3600
        return f"{v} hour{'s' if v != 1 else ''}"
    if secs % 60 == 0:
        v = secs // 60
        return f"{v} minute{'s' if v != 1 else ''}"
    return f"{secs} second{'s' if secs != 1 else ''}"


def _fmt_count(count: int) -> str:
    return "∞" if count == 0 else str(count)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_entry(channel_id: int) -> dict | None:
    return await _col().find_one({"channel_id": channel_id})


async def _upsert_entry(
    guild_id: int,
    channel_id: int,
    message: str,
    interval: int,
    count: int,
) -> None:
    now = datetime.now(timezone.utc)
    await _col().update_one(
        {"channel_id": channel_id},
        {"$set": {
            "guild_id":    guild_id,
            "channel_id":  channel_id,
            "message":     message,
            "interval":    interval,
            "count":       count,     # 0 = infinite
            "sent":        0,
            "paused":      False,
            "created_at":  now,
            "updated_at":  now,
        }},
        upsert=True,
    )


async def _delete_entry(channel_id: int) -> bool:
    result = await _col().delete_one({"channel_id": channel_id})
    return result.deleted_count > 0


async def _set_paused(channel_id: int, paused: bool) -> bool:
    result = await _col().update_one(
        {"channel_id": channel_id},
        {"$set": {"paused": paused, "updated_at": datetime.now(timezone.utc)}},
    )
    return result.matched_count > 0


async def _increment_sent(channel_id: int) -> dict | None:
    return await _col().find_one_and_update(
        {"channel_id": channel_id},
        {"$inc": {"sent": 1}},
        return_document=True,
    )


async def _get_guild_entries(guild_id: int) -> list[dict]:
    return await _col().find({"guild_id": guild_id}).to_list(None)


async def _get_all_entries() -> list[dict]:
    return await _col().find({}).to_list(None)


async def _set_paused_guild(guild_id: int, paused: bool) -> list[int]:
    """Bulk pause/resume all entries in a guild. Returns list of affected channel_ids."""
    docs = await _col().find({"guild_id": guild_id}).to_list(None)
    channel_ids = [d["channel_id"] for d in docs]
    if channel_ids:
        await _col().update_many(
            {"guild_id": guild_id},
            {"$set": {"paused": paused, "updated_at": datetime.now(timezone.utc)}},
        )
    return channel_ids


async def _set_paused_all(paused: bool) -> list[int]:
    """Bulk pause/resume every entry across all guilds. Returns list of affected channel_ids."""
    docs = await _col().find({}).to_list(None)
    channel_ids = [d["channel_id"] for d in docs]
    if channel_ids:
        await _col().update_many(
            {},
            {"$set": {"paused": paused, "updated_at": datetime.now(timezone.utc)}},
        )
    return channel_ids


# ── ensure index ──────────────────────────────────────────────────────────────

async def ensure_repeat_index() -> None:
    db = _db.get_db()
    await db.repeat_messages.create_index([("channel_id", 1)], unique=True)
    await db.repeat_messages.create_index([("guild_id",   1)])


# ── Status embed builder ──────────────────────────────────────────────────────

def _build_status_embed(
    entries: list[dict],
    page: int,
    total_pages: int,
    guild: discord.Guild | None,
    bot: commands.Bot,
    *,
    global_view: bool = False,
) -> discord.Embed:
    title = "🔁 Repeating Messages"
    if global_view:
        title += " — Global"
    elif guild:
        title += f" — {guild.name}"

    embed = discord.Embed(title=title, colour=0x5865F2)
    if not entries:
        embed.description = "*No repeating messages configured.*"
        return embed

    for doc in entries:
        ch_id  = doc["channel_id"]
        g_id   = doc["guild_id"]
        g      = bot.get_guild(g_id)
        ch     = bot.get_channel(ch_id)
        ch_str = ch.mention if ch else f"`#{ch_id}`"

        name_parts = [ch_str]
        if global_view and g:
            name_parts.insert(0, f"**{g.name}** ›")
        field_name = " ".join(name_parts)

        status_icon = "⏸️" if doc.get("paused") else "▶️"
        sent  = doc.get("sent", 0)
        count = doc.get("count", 0)
        remaining = "" if count == 0 else f"  •  {max(0, count - sent)} left"

        preview = doc["message"]
        if len(preview) > 80:
            preview = preview[:77] + "…"

        field_val = (
            f"{status_icon} **Every** {_fmt_interval(doc['interval'])}"
            f"  •  **Count:** {_fmt_count(count)}{remaining}\n"
            f"**Sent:** {sent}\n"
            f"```{preview}```"
        )
        embed.add_field(name=field_name, value=field_val, inline=False)

    embed.set_footer(text=f"Page {page}/{total_pages}")
    return embed


# ── Pagination view ───────────────────────────────────────────────────────────

class StatusPager(discord.ui.View):
    def __init__(
        self,
        all_entries: list[dict],
        guild: discord.Guild | None,
        bot: commands.Bot,
        *,
        global_view: bool = False,
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.all_entries = all_entries
        self.guild       = guild
        self.bot         = bot
        self.global_view = global_view
        self.page        = 1
        self.total_pages = max(1, math.ceil(len(all_entries) / PAGE_SIZE))
        self._update_buttons()

    def _current_page_entries(self) -> list[dict]:
        start = (self.page - 1) * PAGE_SIZE
        return self.all_entries[start : start + PAGE_SIZE]

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= 1
        self.next_btn.disabled = self.page >= self.total_pages

    def build_embed(self) -> discord.Embed:
        return _build_status_embed(
            self._current_page_entries(),
            self.page,
            self.total_pages,
            self.guild,
            self.bot,
            global_view=self.global_view,
        )

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# ── Cog ───────────────────────────────────────────────────────────────────────

class RepeatCog(commands.Cog):
    """Repeating message scheduler — one per channel, auto-resumes on restart."""

    def __init__(self, bot: commands.Bot):
        self.bot   = bot
        # channel_id → asyncio.Task
        self._tasks: dict[int, asyncio.Task] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self):
        await ensure_repeat_index()
        await self._resume_all()

    async def cog_unload(self):
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    # ── internal task management ──────────────────────────────────────────────

    async def _resume_all(self):
        entries = await _get_all_entries()
        for doc in entries:
            if not doc.get("paused", False):
                self._start_task(doc["channel_id"])

    def _start_task(self, channel_id: int):
        self._cancel_task(channel_id)
        task = self.bot.loop.create_task(self._repeat_loop(channel_id))
        self._tasks[channel_id] = task

    def _cancel_task(self, channel_id: int):
        existing = self._tasks.pop(channel_id, None)
        if existing and not existing.done():
            existing.cancel()

    async def _repeat_loop(self, channel_id: int):
        """Core loop: wait interval, send, repeat until done or cancelled."""
        try:
            while True:
                doc = await _get_entry(channel_id)
                if not doc or doc.get("paused"):
                    break

                interval = doc["interval"]
                count    = doc["count"]   # 0 = infinite
                sent     = doc.get("sent", 0)

                # Check if we've hit the limit
                if count > 0 and sent >= count:
                    await _delete_entry(channel_id)
                    break

                await asyncio.sleep(interval)

                # Re-fetch after sleep (may have been removed/paused)
                doc = await _get_entry(channel_id)
                if not doc or doc.get("paused"):
                    break

                channel = self.bot.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    try:
                        await channel.send(doc["message"])
                    except (discord.Forbidden, discord.HTTPException):
                        pass  # silently skip if we lost permissions

                doc = await _increment_sent(channel_id)
                if not doc:
                    break

                # Stop if we've now hit the limit
                count = doc.get("count", 0)
                sent  = doc.get("sent",  0)
                if count > 0 and sent >= count:
                    await _delete_entry(channel_id)
                    break

        except asyncio.CancelledError:
            pass

    # ── shared command logic ──────────────────────────────────────────────────

    async def _cmd_set(
        self,
        guild_id: int,
        channel: discord.TextChannel,
        interval_secs: int,
        count: int,
        message: str,
    ) -> str:
        """Create or replace a repeat entry. Returns a confirmation string."""
        await _upsert_entry(guild_id, channel.id, message, interval_secs, count)
        self._start_task(channel.id)
        count_str = "∞ (infinite)" if count == 0 else str(count)
        return (
            f"✅ Repeating message set in {channel.mention}\n"
            f"**Interval:** {_fmt_interval(interval_secs)}  •  **Repeats:** {count_str}"
        )

    async def _cmd_remove(self, channel: discord.TextChannel) -> str:
        self._cancel_task(channel.id)
        deleted = await _delete_entry(channel.id)
        if deleted:
            return f"✅ Repeating message removed from {channel.mention}."
        return f"⚠️ No repeating message found for {channel.mention}."

    async def _cmd_pause(self, channel: discord.TextChannel) -> str:
        self._cancel_task(channel.id)
        ok = await _set_paused(channel.id, True)
        if ok:
            return f"⏸️ Repeating message paused in {channel.mention}."
        return f"⚠️ No repeating message found for {channel.mention}."

    async def _cmd_resume(self, channel: discord.TextChannel) -> str:
        doc = await _get_entry(channel.id)
        if not doc:
            return f"⚠️ No repeating message found for {channel.mention}."
        await _set_paused(channel.id, False)
        self._start_task(channel.id)
        return f"▶️ Repeating message resumed in {channel.mention}."

    async def _cmd_pauseall(self, scope: str, guild_id: int) -> str:
        """Pause all entries in this guild, or globally (owner use only for 'all')."""
        if scope == "all":
            channel_ids = await _set_paused_all(True)
            for cid in channel_ids:
                self._cancel_task(cid)
            return f"⏸️ Paused **{len(channel_ids)}** repeating message(s) across all servers."
        else:
            channel_ids = await _set_paused_guild(guild_id, True)
            for cid in channel_ids:
                self._cancel_task(cid)
            return f"⏸️ Paused **{len(channel_ids)}** repeating message(s) in this server."

    async def _cmd_resumeall(self, scope: str, guild_id: int) -> str:
        """Resume all entries in this guild, or globally (owner use only for 'all')."""
        if scope == "all":
            channel_ids = await _set_paused_all(False)
            for cid in channel_ids:
                self._start_task(cid)
            return f"▶️ Resumed **{len(channel_ids)}** repeating message(s) across all servers."
        else:
            channel_ids = await _set_paused_guild(guild_id, False)
            for cid in channel_ids:
                self._start_task(cid)
            return f"▶️ Resumed **{len(channel_ids)}** repeating message(s) in this server."

    # ─────────────────────────────────────────────────────────────────────────
    # SLASH COMMANDS
    # ─────────────────────────────────────────────────────────────────────────

    grp = app_commands.Group(
        name="repeat",
        description="Manage repeating messages in channels.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @grp.command(name="set", description="Set a repeating message for a channel.")
    @app_commands.describe(
        channel  = "Channel to send the message in.",
        interval = "How often to repeat (number).",
        unit     = "Unit for the interval.",
        count    = "How many times to repeat (0 or 'inf' for infinite).",
        message  = "The message to send.",
    )
    @app_commands.choices(unit=[
        app_commands.Choice(name="seconds", value="seconds"),
        app_commands.Choice(name="minutes", value="minutes"),
        app_commands.Choice(name="hours",   value="hours"),
        app_commands.Choice(name="days",    value="days"),
    ])
    async def slash_set(
        self,
        interaction: discord.Interaction,
        channel:  discord.TextChannel,
        interval: str,
        unit:     str,
        count:    str,
        message:  str,
    ):
        await interaction.response.defer(ephemeral=True)

        secs = _parse_interval(interval, unit)
        if secs is None:
            await interaction.followup.send(
                f"⚠️ Invalid interval. Minimum is {MIN_INTERVAL} seconds.", ephemeral=True
            )
            return

        cnt = _parse_count(count)
        if cnt is None:
            await interaction.followup.send(
                "⚠️ Invalid count. Use a positive integer, or `0` / `inf` for infinite.",
                ephemeral=True,
            )
            return

        reply = await self._cmd_set(interaction.guild_id, channel, secs, cnt, message)
        await interaction.followup.send(reply, ephemeral=True)

    @grp.command(name="remove", description="Stop and remove a repeating message.")
    @app_commands.describe(channel="Channel whose repeating message to remove.")
    async def slash_remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        reply = await self._cmd_remove(channel)
        await interaction.followup.send(reply, ephemeral=True)

    @grp.command(name="pause", description="Pause a repeating message without deleting it.")
    @app_commands.describe(channel="Channel whose repeating message to pause.")
    async def slash_pause(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        reply = await self._cmd_pause(channel)
        await interaction.followup.send(reply, ephemeral=True)

    @grp.command(name="resume", description="Resume a paused repeating message.")
    @app_commands.describe(channel="Channel whose repeating message to resume.")
    async def slash_resume(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        reply = await self._cmd_resume(channel)
        await interaction.followup.send(reply, ephemeral=True)

    @grp.command(name="status", description="Show all repeating messages in this server.")
    async def slash_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        entries = await _get_guild_entries(interaction.guild_id)
        view    = StatusPager(entries, interaction.guild, self.bot)
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)

    @grp.command(name="globalstatus", description="(Owner only) Show repeating messages across all guilds.")
    async def slash_globalstatus(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self.bot.is_owner(interaction.user):
            await interaction.followup.send("⛔ This command is for the bot owner only.", ephemeral=True)
            return
        entries = await _get_all_entries()
        view    = StatusPager(entries, None, self.bot, global_view=True)
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)

    @grp.command(name="pauseall", description="Pause all repeating messages — this server or every server.")
    @app_commands.describe(scope="'server' to pause this server only, 'all' to pause every server (owner only).")
    @app_commands.choices(scope=[
        app_commands.Choice(name="This server",    value="server"),
        app_commands.Choice(name="All servers 🔒", value="all"),
    ])
    async def slash_pauseall(self, interaction: discord.Interaction, scope: str):
        await interaction.response.defer(ephemeral=True)
        if scope == "all" and not await self.bot.is_owner(interaction.user):
            await interaction.followup.send("⛔ Pausing all servers is restricted to the bot owner.", ephemeral=True)
            return
        reply = await self._cmd_pauseall(scope, interaction.guild_id)
        await interaction.followup.send(reply, ephemeral=True)

    @grp.command(name="resumeall", description="Resume all repeating messages — this server or every server.")
    @app_commands.describe(scope="'server' to resume this server only, 'all' to resume every server (owner only).")
    @app_commands.choices(scope=[
        app_commands.Choice(name="This server",     value="server"),
        app_commands.Choice(name="All servers 🔒",  value="all"),
    ])
    async def slash_resumeall(self, interaction: discord.Interaction, scope: str):
        await interaction.response.defer(ephemeral=True)
        if scope == "all" and not await self.bot.is_owner(interaction.user):
            await interaction.followup.send("⛔ Resuming all servers is restricted to the bot owner.", ephemeral=True)
            return
        reply = await self._cmd_resumeall(scope, interaction.guild_id)
        await interaction.followup.send(reply, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # PREFIX COMMANDS
    # ─────────────────────────────────────────────────────────────────────────

    @commands.group(
        name="repeat",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.has_permissions(manage_guild=True)
    async def pfx_repeat(self, ctx: commands.Context):
        """Repeating messages. Use `a!repeat help` or see subcommands."""
        embed = discord.Embed(
            title="🔁 Repeat Commands",
            description=(
                "`a!repeat set #channel <interval> <unit> <count|inf> <message…>`\n"
                "`a!repeat remove #channel`\n"
                "`a!repeat pause #channel`\n"
                "`a!repeat resume #channel`\n"
                "`a!repeat pauseall [server|all]`\n"
                "`a!repeat resumeall [server|all]`\n"
                "`a!repeat status`\n"
                "`a!repeat globalstatus` *(owner only)*\n\n"
                "**Units:** `s` `m` `h` `d`  (seconds / minutes / hours / days)\n"
                "**Count:** positive integer, or `0` / `inf` for infinite\n"
                "**Scope:** `server` *(default)* or `all` *(owner only)*"
            ),
            colour=0x5865F2,
        )
        await ctx.send(embed=embed)

    @pfx_repeat.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def pfx_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        interval: str,
        unit: str,
        count: str,
        *,
        message: str,
    ):
        """Set a repeating message.
        Usage: a!repeat set #channel <interval> <unit> <count|inf> <message>
        Example: a!repeat set #announcements 30 m 10 Reminder: read the rules!
        """
        secs = _parse_interval(interval, unit)
        if secs is None:
            await ctx.send(f"⚠️ Invalid interval. Minimum is {MIN_INTERVAL} seconds.")
            return

        cnt = _parse_count(count)
        if cnt is None:
            await ctx.send("⚠️ Invalid count. Use a positive integer, or `0`/`inf` for infinite.")
            return

        reply = await self._cmd_set(ctx.guild.id, channel, secs, cnt, message)
        await ctx.send(reply)

    @pfx_repeat.command(name="remove", aliases=["delete", "stop"])
    @commands.has_permissions(manage_guild=True)
    async def pfx_remove(self, ctx: commands.Context, channel: discord.TextChannel):
        """Stop and remove a repeating message. Usage: a!repeat remove #channel"""
        reply = await self._cmd_remove(channel)
        await ctx.send(reply)

    @pfx_repeat.command(name="pause")
    @commands.has_permissions(manage_guild=True)
    async def pfx_pause(self, ctx: commands.Context, channel: discord.TextChannel):
        """Pause a repeating message. Usage: a!repeat pause #channel"""
        reply = await self._cmd_pause(channel)
        await ctx.send(reply)

    @pfx_repeat.command(name="resume")
    @commands.has_permissions(manage_guild=True)
    async def pfx_resume(self, ctx: commands.Context, channel: discord.TextChannel):
        """Resume a paused repeating message. Usage: a!repeat resume #channel"""
        reply = await self._cmd_resume(channel)
        await ctx.send(reply)

    @pfx_repeat.command(name="status", aliases=["list"])
    @commands.has_permissions(manage_guild=True)
    async def pfx_status(self, ctx: commands.Context):
        """Show all repeating messages in this server."""
        entries = await _get_guild_entries(ctx.guild.id)
        view    = StatusPager(entries, ctx.guild, self.bot)
        await ctx.send(embed=view.build_embed(), view=view)

    @pfx_repeat.command(name="globalstatus", aliases=["gstatus"])
    @commands.is_owner()
    async def pfx_globalstatus(self, ctx: commands.Context):
        """(Owner only) Show repeating messages across all guilds."""
        entries = await _get_all_entries()
        view    = StatusPager(entries, None, self.bot, global_view=True)
        await ctx.send(embed=view.build_embed(), view=view)

    @pfx_repeat.command(name="pauseall")
    @commands.has_permissions(manage_guild=True)
    async def pfx_pauseall(self, ctx: commands.Context, scope: str = "server"):
        """Pause all repeating messages. Scope: 'server' (default) or 'all' (owner only).
        Usage: a!repeat pauseall          → pauses this server
               a!repeat pauseall all      → pauses every server (owner only)
        """
        scope = scope.lower()
        if scope not in ("server", "all"):
            await ctx.send("⚠️ Scope must be `server` or `all`.")
            return
        if scope == "all" and not await self.bot.is_owner(ctx.author):
            await ctx.send("⛔ Pausing all servers is restricted to the bot owner.")
            return
        reply = await self._cmd_pauseall(scope, ctx.guild.id)
        await ctx.send(reply)

    @pfx_repeat.command(name="resumeall")
    @commands.has_permissions(manage_guild=True)
    async def pfx_resumeall(self, ctx: commands.Context, scope: str = "server"):
        """Resume all repeating messages. Scope: 'server' (default) or 'all' (owner only).
        Usage: a!repeat resumeall         → resumes this server
               a!repeat resumeall all     → resumes every server (owner only)
        """
        scope = scope.lower()
        if scope not in ("server", "all"):
            await ctx.send("⚠️ Scope must be `server` or `all`.")
            return
        if scope == "all" and not await self.bot.is_owner(ctx.author):
            await ctx.send("⛔ Resuming all servers is restricted to the bot owner.")
            return
        reply = await self._cmd_resumeall(scope, ctx.guild.id)
        await ctx.send(reply)

    # ── error handlers ────────────────────────────────────────────────────────

    @pfx_repeat.error
    @pfx_set.error
    @pfx_remove.error
    @pfx_pause.error
    @pfx_resume.error
    @pfx_pauseall.error
    @pfx_resumeall.error
    @pfx_status.error
    async def _repeat_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("⛔ You need **Manage Server** to use this command.")
        elif isinstance(error, commands.NotOwner):
            await ctx.send("⛔ This command is for the bot owner only.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"⚠️ Missing argument: `{error.param.name}`. See `a!repeat` for usage.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"⚠️ Bad argument: {error}")
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(RepeatCog(bot))
