"""
cogs/autopause_cog.py  —  Auto-pause channels on Rare/Regional Pokémon spawns.

How it works
────────────
1. Listens for messages from NAMING_BOT (configurable per guild via DB).
2. If the message contains "Rare Ping:" or "Regional Ping:" AND "Shortest Name:",
   it schedules a channel lock after `autolock_delay` seconds.
3. On lock: channel permissions are unsynced, Pokétwo loses Send Messages +
   View Channel. A lock notice is posted with the auto-unlock countdown and
   an [Unlock Now] button (usable by anyone).
4. If `autoreminder_delay` is set, a reminder is sent (between lock and unlock)
   pinging the configured role.
5. After `autounlock_delay` seconds the channel is automatically restored.

Collections used (added to db.py helpers at bottom)
────────────────────────────────────────────────────
autopause_config   — per-guild settings
locked_channels    — currently locked channels

Admin commands  (requires Manage Guild)
────────────────────────────────────────
a!autopause enable / disable            — toggle the whole feature
a!autopause status                      — show current settings
a!autopause setlock   <seconds>         — delay before locking  (0 = instant)
a!autopause setunlock <seconds>         — delay before unlocking
a!autopause setreminder <seconds>       — reminder delay (must be between lock & unlock)
a!autopause setrole rare <@role>        — reminder role for rare pings
a!autopause setrole regional <@role>    — reminder role for regional pings
a!autopause setbot <user_id>            — set which bot to listen to (NAMING_BOT)

View commands  (anyone)
────────────────────────
a!locked                                — show currently locked channels
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands

import db

POKETWO_ID = 716390085896962058


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers  (thin wrappers — keep all Mongo code in db.py style)
# ─────────────────────────────────────────────────────────────────────────────

async def _cfg(guild_id: int) -> dict:
    doc = await db.get_db().autopause_config.find_one({"guild_id": guild_id})
    return doc or {}


async def _set_cfg(guild_id: int, **fields) -> None:
    await db.get_db().autopause_config.update_one(
        {"guild_id": guild_id},
        {"$set": fields},
        upsert=True,
    )


async def _add_locked(
    guild_id: int,
    channel_id: int,
    pokemon: str,
    ping_type: str,        # "rare" | "regional"
    message_url: str,
    lock_time: datetime,
    unlock_time: datetime,
) -> None:
    await db.get_db().locked_channels.update_one(
        {"guild_id": guild_id, "channel_id": channel_id},
        {"$set": {
            "pokemon":     pokemon,
            "ping_type":   ping_type,
            "message_url": message_url,
            "lock_time":   lock_time,
            "unlock_time": unlock_time,
        }},
        upsert=True,
    )


async def _remove_locked(guild_id: int, channel_id: int) -> None:
    await db.get_db().locked_channels.delete_one(
        {"guild_id": guild_id, "channel_id": channel_id}
    )


async def _get_locked(guild_id: int) -> list[dict]:
    return await db.get_db().locked_channels.find({"guild_id": guild_id}).to_list(None)


async def _get_locked_entry(guild_id: int, channel_id: int) -> dict | None:
    return await db.get_db().locked_channels.find_one(
        {"guild_id": guild_id, "channel_id": channel_id}
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pokemon(text: str) -> str | None:
    """
    Grab the Pokémon name from the first non-empty line.
    Format: 'Name: 99.99%'  — take everything before the LAST ':'
    e.g.  'Type: Null: 99%' → 'Type: Null'
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            return line.rsplit(":", 1)[0].strip()
    return None


def _detect_ping_type(text: str) -> str | None:
    """Return 'rare', 'regional', or None."""
    if "Rare Ping:" in text:
        return "rare"
    if "Regional Ping:" in text:
        return "regional"
    return None


def _fmt_seconds(s: int) -> str:
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if sec == 0:
        return f"{m}m"
    return f"{m}m {sec}s"


# ─────────────────────────────────────────────────────────────────────────────
# Persistent Unlock Button View
# ─────────────────────────────────────────────────────────────────────────────

class UnlockView(discord.ui.View):
    """
    A persistent view with a single 'Unlock Now' button.
    custom_id encodes guild_id and channel_id so it survives restarts.
    """

    def __init__(self):
        super().__init__(timeout=None)   # persistent

    @discord.ui.button(
        label="🔓 Unlock Now",
        style=discord.ButtonStyle.success,
        custom_id="autopause:unlock",
    )
    async def unlock_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        channel = interaction.channel
        guild   = interaction.guild

        # Pull the cog to call shared unlock logic
        cog: AutopauseCog | None = interaction.client.get_cog("AutopauseCog")
        if cog is None:
            await interaction.response.send_message(
                "❌ Autopause cog not loaded.", ephemeral=True
            )
            return

        entry = await _get_locked_entry(guild.id, channel.id)
        if not entry:
            await interaction.response.send_message(
                "ℹ️ This channel is not marked as locked.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await cog.do_unlock(guild, channel, reason="Manual unlock via button")
        await interaction.followup.send("✅ Channel unlocked.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Locked-channels paginator
# ─────────────────────────────────────────────────────────────────────────────

PAGE = 5   # entries per page


class LockedView(discord.ui.View):
    def __init__(self, cog: "AutopauseCog", guild: discord.Guild, entries: list[dict]):
        super().__init__(timeout=120)
        self.cog     = cog
        self.guild   = guild
        self.entries = entries
        self.mode    = "rare"      # "rare" | "regional"
        self.page    = 0
        self._refresh_buttons()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _filtered(self) -> list[dict]:
        return [e for e in self.entries if e["ping_type"] == self.mode]

    def _total_pages(self) -> int:
        return max(1, (len(self._filtered()) + PAGE - 1) // PAGE)

    def _refresh_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self._total_pages() - 1

    def _build_embed(self) -> discord.Embed:
        items = self._filtered()
        colour = discord.Color.red() if self.mode == "rare" else discord.Color.purple()
        label  = "🌟 Rare" if self.mode == "rare" else "🗺️ Regional"
        embed  = discord.Embed(
            title=f"{label} — Locked Channels",
            color=colour,
        )
        if not items:
            embed.description = "*No channels currently locked.*"
            return embed

        chunk = items[self.page * PAGE : (self.page + 1) * PAGE]
        lines = []
        for i, e in enumerate(chunk, start=self.page * PAGE + 1):
            ch   = self.guild.get_channel(e["channel_id"])
            ch_s = f"[#{ch.name}]({e['message_url']})" if ch else f"`{e['channel_id']}`"
            unlock_ts = int(e["unlock_time"].timestamp())
            lines.append(
                f"**{i}.** {e['pokemon']} in {ch_s} — unlocks <t:{unlock_ts}:R>"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Page {self.page + 1}/{self._total_pages()}")
        return embed

    # ── buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="🌟 Rare", style=discord.ButtonStyle.danger, row=0)
    async def rare_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.mode = "rare"
        self.page = 0
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="🗺️ Regional", style=discord.ButtonStyle.primary, row=0)
    async def regional_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.mode = "regional"
        self.page = 0
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page -= 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page += 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="🔓 Unlock All", style=discord.ButtonStyle.success, row=1)
    async def unlock_all_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ):
        items = self._filtered()
        if not items:
            await interaction.response.send_message(
                "ℹ️ Nothing to unlock.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        count = 0
        for e in items:
            ch = self.guild.get_channel(e["channel_id"])
            if ch:
                await self.cog.do_unlock(
                    self.guild, ch, reason=f"Bulk unlock via a!locked by {interaction.user}"
                )
                count += 1
        # Refresh entries
        self.entries = await _get_locked(self.guild.id)
        self.page    = 0
        self._refresh_buttons()
        await interaction.message.edit(embed=self._build_embed(), view=self)
        await interaction.followup.send(f"✅ Unlocked {count} channel(s).", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Main Cog
# ─────────────────────────────────────────────────────────────────────────────

class AutopauseCog(commands.Cog, name="AutopauseCog"):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Task tracking: (guild_id, channel_id) → asyncio.Task
        # These store tasks that are actively running and need cleanup
        self._active_tasks: dict[str, asyncio.Task] = {}  # "lock" | "unlock" | "reminder" + key
        # guild_id → Member cache for Pokétwo
        self._poketwo: dict[int, discord.Member] = {}

    async def _get_poketwo(self, guild: discord.Guild) -> discord.Member | None:
        """Return the Pokétwo Member for this guild, fetching and caching if needed."""
        if guild.id in self._poketwo:
            return self._poketwo[guild.id]
        try:
            member = await guild.fetch_member(POKETWO_ID)
            self._poketwo[guild.id] = member
            print(f"[autopause] fetched and cached Pokétwo for guild {guild.id}")
            return member
        except discord.NotFound:
            print(f"[autopause] Pokétwo not found in guild {guild.id}")
            return None
        except Exception as e:
            print(f"[autopause] fetch_member failed: {e}")
            return None

    def _task_key(self, task_type: str, guild_id: int, channel_id: int) -> str:
        """Build a unique key for a task."""
        return f"{task_type}:{guild_id}:{channel_id}"

    def _get_task(self, key: str) -> asyncio.Task | None:
        """Retrieve a task by key."""
        return self._active_tasks.get(key)

    def _set_task(self, key: str, task: asyncio.Task) -> None:
        """Store a task and cancel any previous one with the same key."""
        old_task = self._active_tasks.get(key)
        if old_task and not old_task.done():
            print(f"[autopause] cancelling previous task: {key}")
            old_task.cancel()
        self._active_tasks[key] = task
        print(f"[autopause] task set: {key}")

    def _clear_task(self, key: str) -> None:
        """Remove a task from tracking. Don't cancel the currently executing task."""
        task = self._active_tasks.pop(key, None)
        if task and not task.done():
            try:
                current = asyncio.current_task()
                # Don't cancel the task that's currently executing
                if task is not current:
                    task.cancel()
                    print(f"[autopause] task cleared and cancelled: {key}")
                else:
                    print(f"[autopause] task cleared (skipped self-cancel): {key}")
            except RuntimeError:
                # No current task (not in async context)
                task.cancel()
                print(f"[autopause] task cleared and cancelled: {key}")
        else:
            print(f"[autopause] task cleared: {key}")

    async def cog_load(self):
        # Register the persistent view so buttons work after restarts
        self.bot.add_view(UnlockView())
        # Recover any channels that were locked before a restart
        asyncio.create_task(self._recover_locked_channels())

    async def _recover_locked_channels(self):
        """
        On startup, find every channel still marked as locked in the DB.
        If the unlock time has already passed → unlock immediately.
        If it's still in the future → reschedule the unlock task.
        """
        # Wait until the bot is fully ready so guild/channel caches are populated
        await self.bot.wait_until_ready()
        print("[autopause] _recover_locked_channels: bot ready, scanning guilds...")

        # Pre-fetch Pokétwo member for all guilds so it's cached before any lock/unlock
        for guild in self.bot.guilds:
            await self._get_poketwo(guild)

        now = datetime.now(timezone.utc)

        # Iterate over every guild the bot is in
        for guild in self.bot.guilds:
            entries = await _get_locked(guild.id)
            print(f"[autopause] Guild {guild.id} ({guild.name}): found {len(entries)} locked entry/entries")
            for entry in entries:
                channel = guild.get_channel(entry["channel_id"])
                if channel is None:
                    print(f"[autopause]   channel {entry['channel_id']} not found — removing from DB")
                    # Channel deleted — clean up DB entry
                    await _remove_locked(guild.id, entry["channel_id"])
                    continue

                unlock_time = entry.get("unlock_time")
                print(f"[autopause]   #{channel.name} ({channel.id}): unlock_time={unlock_time!r} tzinfo={getattr(unlock_time, 'tzinfo', 'N/A')}")
                if unlock_time is None:
                    print(f"[autopause]   unlock_time is None — skipping (no autounlock configured)")
                    continue

                # MongoDB strips tzinfo — make it UTC-aware so subtraction works
                if unlock_time.tzinfo is None:
                    print(f"[autopause]   unlock_time is timezone-naive — patching to UTC")
                    unlock_time = unlock_time.replace(tzinfo=timezone.utc)

                remaining = (unlock_time - now).total_seconds()
                print(f"[autopause]   remaining={remaining:.1f}s")

                if remaining <= 0:
                    print(f"[autopause]   overdue — unlocking now")
                    # Overdue — unlock now
                    await self.do_unlock(
                        guild, channel, reason="Auto-unlock: overdue after restart"
                    )
                else:
                    print(f"[autopause]   rescheduling unlock in {remaining:.1f}s")
                    # Still pending — reschedule
                    key = self._task_key("unlock", guild.id, channel.id)
                    task = asyncio.create_task(
                        self._schedule_unlock(guild, channel, int(remaining))
                    )
                    self._set_task(key, task)

    # ── Listener ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        cfg = await _cfg(message.guild.id)

        # Feature toggle
        if not cfg.get("enabled", False):
            return

        # ── Pokétwo catch → cancel any pending lock for this channel ──────────
        if message.author.id == POKETWO_ID:
            text = message.content or ""
            if "Congratulations" in text:
                # Someone caught the Pokémon — cancel the lock
                key = self._task_key("lock", message.guild.id, message.channel.id)
                self._clear_task(key)
            return

        # ── Naming bot spawn ping ─────────────────────────────────────────────
        naming_bot_id = cfg.get("naming_bot_id")
        if not naming_bot_id or message.author.id != naming_bot_id:
            return

        text = message.content or ""
        if "Shortest Name:" not in text:
            return

        ping_type = _detect_ping_type(text)
        if not ping_type:
            return

        pokemon = _extract_pokemon(text)
        if not pokemon:
            return

        autolock_delay = cfg.get("autolock_delay")    # seconds or None
        if autolock_delay is None:
            return   # not configured yet

        # Cancel any pending lock for this channel (new spawn supersedes old)
        key = self._task_key("lock", message.guild.id, message.channel.id)
        self._clear_task(key)

        task = asyncio.create_task(
            self._schedule_lock(message, pokemon, ping_type, autolock_delay, cfg)
        )
        self._set_task(key, task)

    # ── Scheduled lock ────────────────────────────────────────────────────────

    async def _schedule_lock(
        self,
        trigger_msg: discord.Message,
        pokemon:     str,
        ping_type:   str,
        delay:       int,
        cfg:         dict,
    ):
        """Wait `delay` seconds then lock the channel."""
        guild   = trigger_msg.guild
        channel = trigger_msg.channel
        key     = self._task_key("lock", guild.id, channel.id)

        try:
            if delay > 0:
                await asyncio.sleep(delay)

            # Re-fetch config in case it changed while we slept
            cfg = await _cfg(guild.id)
            if not cfg.get("enabled", False):
                print(f"[autopause] _schedule_lock: feature disabled, aborting lock for #{channel.name}")
                return

            autounlock_delay = cfg.get("autounlock_delay")

            now         = datetime.now(timezone.utc)
            unlock_time = None
            if autounlock_delay is not None:
                unlock_time = now + timedelta(seconds=autounlock_delay)

            # Build lock message
            lock_parts = [
                f"🔒 **This channel has been locked** because no one caught "
                f"**{pokemon}** within **{_fmt_seconds(delay)}**."
            ]
            if unlock_time:
                unlock_ts = int(unlock_time.timestamp())
                lock_parts.append(
                    f"This channel will automatically unlock <t:{unlock_ts}:R>."
                )
            lock_parts.append(
                "-# Use the button below to unlock early. "
                "If the button doesn't work, use `a!unlock` or `a!u` in this channel."
            )

            # Lock the channel
            await self._lock_channel(guild, channel)

            # Store in DB
            await _add_locked(
                guild_id    = guild.id,
                channel_id  = channel.id,
                pokemon     = pokemon,
                ping_type   = ping_type,
                message_url = trigger_msg.jump_url,
                lock_time   = now,
                unlock_time = unlock_time,
            )

            # Send notice with persistent unlock button
            try:
                view = UnlockView()
                await channel.send("\n".join(lock_parts), view=view)
            except discord.Forbidden:
                print(f"[autopause] _schedule_lock: could not send lock message in #{channel.name}")

            # Schedule reminder
            reminder_delay = cfg.get("autoreminder_delay")
            if (
                reminder_delay is not None
                and autounlock_delay is not None
                and delay < reminder_delay < delay + autounlock_delay
            ):
                reminder_key = self._task_key("reminder", guild.id, channel.id)
                reminder_task = asyncio.create_task(
                    self._schedule_reminder(
                        guild, channel, pokemon, ping_type, cfg,
                        # reminder fires `reminder_delay - delay` seconds after lock
                        reminder_delay - delay,
                    )
                )
                self._set_task(reminder_key, reminder_task)

            # Schedule auto-unlock
            if autounlock_delay is not None:
                unlock_key = self._task_key("unlock", guild.id, channel.id)
                print(f"[autopause] _schedule_lock: scheduling auto-unlock for #{channel.name} in {autounlock_delay}s")
                unlock_task = asyncio.create_task(
                    self._schedule_unlock(guild, channel, autounlock_delay)
                )
                self._set_task(unlock_key, unlock_task)
                print(f"[autopause] _schedule_lock: unlock task scheduled: {unlock_task}")
            else:
                print(f"[autopause] _schedule_lock: autounlock_delay is None — no auto-unlock scheduled")

        except asyncio.CancelledError:
            print(f"[autopause] _schedule_lock: lock task cancelled for #{channel.name}")
            raise
        except Exception as e:
            print(f"[autopause] _schedule_lock: unexpected error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Clean up the lock task tracking when it completes
            self._clear_task(key)

    # ── Lock / unlock channel ─────────────────────────────────────────────────

    async def _lock_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        """Unsync + remove Pokétwo perms."""
        print(f"[autopause] _lock_channel: locking #{channel.name}")
        poketwo = await self._get_poketwo(guild)
        if poketwo is None:
            print(f"[autopause] _lock_channel: could not get Pokétwo member, aborting")
            return
        try:
            overwrites = dict(channel.overwrites)
            overwrites[poketwo] = discord.PermissionOverwrite(send_messages=False, view_channel=False)
            await channel.edit(overwrites=overwrites, reason="Autopause: rare/regional Pokémon locked")
            print(f"[autopause] _lock_channel: locked #{channel.name} successfully")
        except discord.Forbidden:
            print(f"[autopause] _lock_channel: Forbidden — bot lacks permission to edit #{channel.name}")
        except Exception as e:
            print(f"[autopause] _lock_channel: unexpected error: {type(e).__name__}: {e}")

    async def _unlock_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        """Remove Pokétwo's deny overwrite to unlock the channel."""
        print(f"[autopause] _unlock_channel: unlocking #{channel.name}")
        poketwo = await self._get_poketwo(guild)
        if poketwo is None:
            print(f"[autopause] _unlock_channel: could not get Pokétwo member, aborting")
            return

        try:
            # Simply remove the Pokétwo overwrite if it exists
            # This lets Pokétwo revert to default role permissions
            overwrites = dict(channel.overwrites)
            if poketwo in overwrites:
                print(f"[autopause] _unlock_channel: removing Pokétwo overwrite from #{channel.name}")
                del overwrites[poketwo]
            else:
                print(f"[autopause] _unlock_channel: Pokétwo has no overwrite in #{channel.name}, skipping edit")
                return

            print(f"[autopause] _unlock_channel: applying overwrites (removed Pokétwo entry), total={len(overwrites)}")
            # Add timeout to prevent hanging
            await asyncio.wait_for(
                channel.edit(overwrites=overwrites, reason="Autopause: channel unlocked"),
                timeout=5.0
            )
            print(f"[autopause] _unlock_channel: overwrite removed successfully for #{channel.name}")
        except asyncio.TimeoutError:
            print(f"[autopause] _unlock_channel: TIMEOUT — channel.edit() took too long for #{channel.name}")
        except discord.Forbidden as e:
            print(f"[autopause] _unlock_channel: Forbidden — bot lacks permission to edit #{channel.name}")
            print(f"[autopause] _unlock_channel: error details: {e}")
        except discord.HTTPException as e:
            print(f"[autopause] _unlock_channel: HTTPException: {getattr(e, 'status', '?')} {getattr(e, 'code', '?')} - {getattr(e, 'text', str(e))}")
        except Exception as e:
            print(f"[autopause] _unlock_channel: unexpected error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    async def do_unlock(
        self,
        guild:   discord.Guild,
        channel: discord.TextChannel,
        *,
        reason:  str = "Autopause unlock",
    ):
        """Shared unlock logic used by tasks, button, and bulk-unlock."""
        print(f"[autopause] do_unlock: called for #{channel.name} | reason={reason!r}")

        # Cancel all pending tasks for this channel (unlock, reminder, any pending locks)
        for task_type in ("unlock", "reminder", "lock"):
            key = self._task_key(task_type, guild.id, channel.id)
            self._clear_task(key)

        print(f"[autopause] do_unlock: calling _unlock_channel for #{channel.name}")
        await self._unlock_channel(guild, channel)
        print(f"[autopause] do_unlock: removing DB entry for #{channel.name}")
        await _remove_locked(guild.id, channel.id)
        print(f"[autopause] do_unlock: done for #{channel.name}")

        try:
            await channel.send(f"🔓 **Channel unlocked.** ({reason})")
        except discord.Forbidden:
            pass

    # ── Scheduled unlock ──────────────────────────────────────────────────────

    async def _schedule_unlock(
        self, guild: discord.Guild, channel: discord.TextChannel, delay: int
    ):
        """Wait `delay` seconds then unlock the channel."""
        key = self._task_key("unlock", guild.id, channel.id)
        print(f"[autopause] _schedule_unlock: will unlock #{channel.name} in {delay}s")

        try:
            await asyncio.sleep(delay)
            print(f"[autopause] _schedule_unlock: sleep done for #{channel.name}, checking DB entry...")
            entry = await _get_locked_entry(guild.id, channel.id)
            if entry:
                print(f"[autopause] _schedule_unlock: entry found — calling do_unlock for #{channel.name}")
                await self.do_unlock(guild, channel, reason="Auto-unlock timer expired")
            else:
                print(f"[autopause] _schedule_unlock: no DB entry for #{channel.name} — already unlocked, skipping")
        except asyncio.CancelledError:
            # Task was cancelled externally (e.g., manual unlock)
            raise
        except Exception as e:
            print(f"[autopause] _schedule_unlock: unexpected error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Clean up task tracking (just remove from dict, don't cancel self)
            self._active_tasks.pop(key, None)

    # ── Scheduled reminder ────────────────────────────────────────────────────

    async def _schedule_reminder(
        self,
        guild:      discord.Guild,
        channel:    discord.TextChannel,
        pokemon:    str,
        ping_type:  str,
        cfg:        dict,
        delay:      int,
    ):
        """Wait `delay` seconds then post a reminder."""
        key = self._task_key("reminder", guild.id, channel.id)

        try:
            await asyncio.sleep(delay)
            entry = await _get_locked_entry(guild.id, channel.id)
            if not entry:
                print(f"[autopause] _schedule_reminder: channel already unlocked, skipping reminder for #{channel.name}")
                return   # already unlocked

            role_key = f"reminder_role_{ping_type}"
            role_id  = cfg.get(role_key)
            role_mention = f"<@&{role_id}>" if role_id else ""

            try:
                await channel.send(
                    f"**{pokemon}:**\n{role_mention}\n-# Reminder: this channel was locked because no one caught it.",
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                print(f"[autopause] _schedule_reminder: reminder sent for #{channel.name}")
            except discord.Forbidden:
                print(f"[autopause] _schedule_reminder: could not send reminder in #{channel.name}")
        except asyncio.CancelledError:
            print(f"[autopause] _schedule_reminder: reminder task cancelled for #{channel.name}")
            raise
        except Exception as e:
            print(f"[autopause] _schedule_reminder: unexpected error: {type(e).__name__}: {e}")
        finally:
            # Clean up task tracking (just remove from dict, don't cancel self)
            self._active_tasks.pop(key, None)

    # ─────────────────────────────────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────────────────────────────────

    # ── a!autopause ───────────────────────────────────────────────────────────

    @commands.group(name="autopause", aliases=["ap"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def autopause(self, ctx: commands.Context):
        """Autopause settings. Use `a!autopause status` to see current config."""
        await ctx.send_help(ctx.command)

    @autopause.command(name="enable")
    @commands.has_permissions(manage_guild=True)
    async def ap_enable(self, ctx: commands.Context):
        """Enable the autopause feature for this server."""
        await _set_cfg(ctx.guild.id, enabled=True)
        await ctx.reply("✅ Autopause **enabled**.")

    @autopause.command(name="disable")
    @commands.has_permissions(manage_guild=True)
    async def ap_disable(self, ctx: commands.Context):
        """Disable the autopause feature for this server."""
        await _set_cfg(ctx.guild.id, enabled=False)
        await ctx.reply("✅ Autopause **disabled**.")

    @autopause.command(name="status")
    @commands.has_permissions(manage_guild=True)
    async def ap_status(self, ctx: commands.Context):
        """Show current autopause configuration."""
        cfg = await _cfg(ctx.guild.id)
        enabled = cfg.get("enabled", False)

        def _role(key):
            rid = cfg.get(key)
            if rid:
                r = ctx.guild.get_role(rid)
                return r.mention if r else f"`{rid}`"
            return "*not set*"

        def _delay(key):
            v = cfg.get(key)
            return _fmt_seconds(v) if v is not None else "*not set*"

        naming_bot_id = cfg.get("naming_bot_id")
        nb_str = f"<@{naming_bot_id}> (`{naming_bot_id}`)" if naming_bot_id else "*not set*"

        e = discord.Embed(
            title="⚙️ Autopause Configuration",
            color=discord.Color.green() if enabled else discord.Color.red(),
        )
        e.add_field(name="Status",           value="🟢 Enabled" if enabled else "🔴 Disabled", inline=False)
        e.add_field(name="Naming Bot",       value=nb_str,                         inline=False)
        e.add_field(name="Auto-lock delay",  value=_delay("autolock_delay"),        inline=True)
        e.add_field(name="Auto-unlock delay",value=_delay("autounlock_delay"),      inline=True)
        e.add_field(name="Reminder delay",   value=_delay("autoreminder_delay"),    inline=True)
        e.add_field(name="Rare role",        value=_role("reminder_role_rare"),     inline=True)
        e.add_field(name="Regional role",    value=_role("reminder_role_regional"), inline=True)
        await ctx.reply(embed=e)

    @autopause.command(name="setlock")
    @commands.has_permissions(manage_guild=True)
    async def ap_setlock(self, ctx: commands.Context, seconds: int):
        """Set delay (in seconds) before locking. Use 0 for instant."""
        if seconds < 0:
            await ctx.reply("❌ Value must be ≥ 0.")
            return
        await _set_cfg(ctx.guild.id, autolock_delay=seconds)
        await ctx.reply(
            f"✅ Auto-lock delay set to **{_fmt_seconds(seconds)}**."
        )

    @autopause.command(name="setunlock")
    @commands.has_permissions(manage_guild=True)
    async def ap_setunlock(self, ctx: commands.Context, seconds: int):
        """Set delay (in seconds) before auto-unlocking after lock."""
        if seconds <= 0:
            await ctx.reply("❌ Value must be > 0.")
            return
        await _set_cfg(ctx.guild.id, autounlock_delay=seconds)
        await ctx.reply(
            f"✅ Auto-unlock delay set to **{_fmt_seconds(seconds)}**."
        )

    @autopause.command(name="setreminder")
    @commands.has_permissions(manage_guild=True)
    async def ap_setreminder(self, ctx: commands.Context, seconds: int):
        """
        Set reminder delay (in seconds from spawn detection).
        Must be between autolock_delay and autolock_delay + autounlock_delay.
        """
        cfg = await _cfg(ctx.guild.id)
        lock_d   = cfg.get("autolock_delay")
        unlock_d = cfg.get("autounlock_delay")

        if lock_d is None or unlock_d is None:
            await ctx.reply(
                "❌ Set both `autolock` and `autounlock` delays first."
            )
            return

        if not (lock_d < seconds < lock_d + unlock_d):
            await ctx.reply(
                f"❌ Reminder delay must be between **{_fmt_seconds(lock_d)}** "
                f"(lock) and **{_fmt_seconds(lock_d + unlock_d)}** (unlock). "
                f"Got: **{_fmt_seconds(seconds)}**."
            )
            return

        await _set_cfg(ctx.guild.id, autoreminder_delay=seconds)
        await ctx.reply(
            f"✅ Reminder delay set to **{_fmt_seconds(seconds)}** after spawn detection."
        )

    @autopause.command(name="setrole")
    @commands.has_permissions(manage_guild=True)
    async def ap_setrole(self, ctx: commands.Context, ping_type: str, role: discord.Role):
        """
        Set the reminder ping role.
        Usage: a!autopause setrole rare @Role
               a!autopause setrole regional @Role
        """
        ping_type = ping_type.lower()
        if ping_type not in ("rare", "regional"):
            await ctx.reply("❌ Type must be `rare` or `regional`.")
            return
        key = f"reminder_role_{ping_type}"
        await _set_cfg(ctx.guild.id, **{key: role.id})
        await ctx.reply(
            f"✅ **{ping_type.capitalize()}** reminder role set to {role.mention}."
        )

    @autopause.command(name="setbot")
    @commands.has_permissions(manage_guild=True)
    async def ap_setbot(self, ctx: commands.Context, bot_id: int):
        """Set the Naming Bot user ID to listen to."""
        await _set_cfg(ctx.guild.id, naming_bot_id=bot_id)
        await ctx.reply(f"✅ Naming bot set to `{bot_id}`.")

    # ── a!locked ──────────────────────────────────────────────────────────────

    @commands.command(name="locked")
    async def locked(self, ctx: commands.Context):
        """Show currently locked channels (rare & regional)."""
        entries = await _get_locked(ctx.guild.id)
        view    = LockedView(self, ctx.guild, entries)
        await ctx.reply(embed=view._build_embed(), view=view)

    # ── a!unlock ──────────────────────────────────────────────────────────────

    @commands.command(name="unlock", aliases=["u"])
    async def unlock_cmd(self, ctx: commands.Context):
        """Manually unlock the current channel if it was locked by autopause."""
        entry = await _get_locked_entry(ctx.guild.id, ctx.channel.id)
        if not entry:
            await ctx.reply("ℹ️ This channel is not currently locked by autopause.")
            return
        await self.do_unlock(
            ctx.guild, ctx.channel,
            reason=f"Manual unlock by {ctx.author} via a!unlock",
        )

    # ── Error handler ─────────────────────────────────────────────────────────

    @autopause.error
    async def ap_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(AutopauseCog(bot))
