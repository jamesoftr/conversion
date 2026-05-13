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
a!autopause setlogchannel #channel      — set channel where lock logs are posted

View commands  (anyone)
────────────────────────
a!locked                                — show currently locked channels

Collection / ping commands  (anyone)
─────────────────────────────────────
a!pings add <Pokemon names…>            — add Pokémon to your personal ping collection
a!pings remove <Pokemon names…>         — remove Pokémon from your collection
a!pings clear                           — clear your entire collection
a!pings list                            — show your current collection

When a channel is locked, anyone who has that Pokémon in their collection
will be @mentioned at the top of the lock-log embed posted to the log channel.

Collections used (added to db.py helpers at bottom)
────────────────────────────────────────────────────
autopause_config   — per-guild settings  (log_channel_id field added)
locked_channels    — currently locked channels
user_pings         — per-guild per-user Pokémon ping collections
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands

import db
import pokedata

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




async def _get_custom_list(guild_id: int) -> list[str]:
    """Get custom Pokémon list for this guild."""
    return await db.get_custom_pokemon_list(guild_id)


async def _add_to_custom_list(guild_id: int, pokemon: str) -> bool:
    """Add Pokémon to custom list. Returns True if added."""
    return await db.add_custom_pokemon(guild_id, pokemon)


async def _remove_from_custom_list(guild_id: int, pokemon: str) -> bool:
    """Remove Pokémon from custom list. Returns True if removed."""
    return await db.remove_custom_pokemon(guild_id, pokemon)


async def _clear_custom_list(guild_id: int) -> None:
    """Clear entire custom list."""
    return await db.clear_custom_pokemon_list(guild_id)


async def _add_locked(
    guild_id: int,
    channel_id: int,
    pokemon: str,
    ping_type: str,        # "rare" | "regional" | "custom"
    message_url: str,
    lock_time: datetime,
    unlock_time: datetime,
    is_custom: bool = False,
) -> None:
    await db.get_db().locked_channels.update_one(
        {"guild_id": guild_id, "channel_id": channel_id},
        {"$set": {
            "pokemon":     pokemon,
            "ping_type":   ping_type,
            "message_url": message_url,
            "lock_time":   lock_time,
            "unlock_time": unlock_time,
            "is_custom":   is_custom,
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


# ── User ping-collection helpers ──────────────────────────────────────────────

async def _get_user_pings(guild_id: int, user_id: int) -> list[str]:
    """Return the list of Pokémon names a user is tracking in this guild."""
    doc = await db.get_db().user_pings.find_one(
        {"guild_id": guild_id, "user_id": user_id}
    )
    return doc.get("pokemon", []) if doc else []


async def _add_user_pings(guild_id: int, user_id: int, names: list[str]) -> list[str]:
    """
    Add Pokémon names (case-insensitive dedup) to the user's collection.
    Returns the list of names that were actually added (not already present).
    """
    doc = await db.get_db().user_pings.find_one(
        {"guild_id": guild_id, "user_id": user_id}
    )
    existing: list[str] = doc.get("pokemon", []) if doc else []
    existing_lower = {p.lower() for p in existing}

    added = []
    for name in names:
        if name.lower() not in existing_lower:
            existing.append(name)
            existing_lower.add(name.lower())
            added.append(name)

    await db.get_db().user_pings.update_one(
        {"guild_id": guild_id, "user_id": user_id},
        {"$set": {"pokemon": existing}},
        upsert=True,
    )
    return added


async def _remove_user_pings(guild_id: int, user_id: int, names: list[str]) -> list[str]:
    """
    Remove Pokémon names from the user's collection.
    Returns the list of names that were actually removed.
    """
    doc = await db.get_db().user_pings.find_one(
        {"guild_id": guild_id, "user_id": user_id}
    )
    existing: list[str] = doc.get("pokemon", []) if doc else []
    names_lower = {n.lower() for n in names}

    removed = [p for p in existing if p.lower() in names_lower]
    remaining = [p for p in existing if p.lower() not in names_lower]

    await db.get_db().user_pings.update_one(
        {"guild_id": guild_id, "user_id": user_id},
        {"$set": {"pokemon": remaining}},
        upsert=True,
    )
    return removed


async def _clear_user_pings(guild_id: int, user_id: int) -> int:
    """Clear all Pokémon from a user's collection. Returns count removed."""
    doc = await db.get_db().user_pings.find_one(
        {"guild_id": guild_id, "user_id": user_id}
    )
    count = len(doc.get("pokemon", [])) if doc else 0
    await db.get_db().user_pings.update_one(
        {"guild_id": guild_id, "user_id": user_id},
        {"$set": {"pokemon": []}},
        upsert=True,
    )
    return count


async def _find_users_with_pokemon(guild_id: int, pokemon: str) -> list[int]:
    """
    Return a list of user_ids who have `pokemon` in their collection.
    Matching is case-insensitive against both the exact name and base name.
    """
    pokemon_lower = pokemon.lower()
    cursor = db.get_db().user_pings.find({"guild_id": guild_id})
    docs = await cursor.to_list(None)
    return [
        doc["user_id"]
        for doc in docs
        if any(p.lower() == pokemon_lower for p in doc.get("pokemon", []))
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pokemon(text: str) -> str | None:
    """
    Grab the Pokémon name from the first non-empty line.
    Format: 'Name: 99.99%'  — take everything before the LAST ':'
    e.g.  'Type: Null: 99%' → 'Type: Null'
    e.g.  'Cicada Vikavolt: 99%' → 'Cicada Vikavolt' (full form name)
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            return line.rsplit(":", 1)[0].strip()
    return None


def _get_base_pokemon(pokemon_name: str) -> str:
    """
    Extract base Pokémon name from form variants.
    e.g. 'Cicada Vikavolt' → 'Vikavolt'
    e.g. 'Alolan Exeggutor' → 'Exeggutor'
    e.g. 'Mega Charizard X' → 'Charizard'

    Uses pokedata to validate — if the full name exists, return it.
    Otherwise, try to extract the last word.
    """
    # Try exact match first
    if pokedata.get(pokemon_name):
        return pokemon_name

    # Try the last word (handles form prefixes like "Cicada Vikavolt" → "Vikavolt")
    words = pokemon_name.split()
    if len(words) > 1:
        base = words[-1]
        if pokedata.get(base):
            return base

    # If still not found, return original
    return pokemon_name


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
        if self.mode == "custom":
            return [e for e in self.entries if e.get("is_custom", False)]
        return [e for e in self.entries if e["ping_type"] == self.mode and not e.get("is_custom", False)]

    def _total_pages(self) -> int:
        return max(1, (len(self._filtered()) + PAGE - 1) // PAGE)

    def _refresh_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self._total_pages() - 1

        # Disable category buttons if they have no items
        rare_items = [e for e in self.entries if e["ping_type"] == "rare" and not e.get("is_custom", False)]
        regional_items = [e for e in self.entries if e["ping_type"] == "regional" and not e.get("is_custom", False)]
        custom_items = [e for e in self.entries if e.get("is_custom", False)]

        self.rare_btn.disabled = len(rare_items) == 0
        self.regional_btn.disabled = len(regional_items) == 0
        self.custom_btn.disabled = len(custom_items) == 0

    def _build_embed(self) -> discord.Embed:
        items = self._filtered()
        if self.mode == "custom":
            colour = discord.Color.gold()
            label = "⭐ Custom"
        elif self.mode == "rare":
            colour = discord.Color.red()
            label = "🌟 Rare"
        else:
            colour = discord.Color.purple()
            label = "🗺️ Regional"
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

    @discord.ui.button(label="⭐ Custom", style=discord.ButtonStyle.success, row=0)
    async def custom_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.mode = "custom"
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

        pokemon = _extract_pokemon(text)
        if not pokemon:
            return

        ping_type = _detect_ping_type(text)  # "rare", "regional", or None
        is_custom_pokemon = False

        # If not rare/regional, check custom list
        if not ping_type:
            custom_list = await _get_custom_list(message.guild.id)
            pokemon_match = any(pokemon.lower() == p.lower() for p in custom_list)
            if pokemon_match:
                print(f"[autopause] spawn detected: {pokemon} in custom list, will trigger lock")
                ping_type = "custom"
                is_custom_pokemon = True
            else:
                print(f"[autopause] spawn detected: {pokemon} not rare/regional/custom, skipping")
                return
        else:
            # It's rare or regional — always proceed regardless of custom list
            print(f"[autopause] spawn detected: {pokemon} is {ping_type}, will trigger lock")

        autolock_delay = cfg.get("autolock_delay")    # seconds or None
        if autolock_delay is None:
            return   # not configured yet

        # Cancel any pending lock for this channel (new spawn supersedes old)
        key = self._task_key("lock", message.guild.id, message.channel.id)
        self._clear_task(key)

        task = asyncio.create_task(
            self._schedule_lock(message, pokemon, ping_type, autolock_delay, cfg, is_custom_pokemon)
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
        is_custom:   bool = False,
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
                is_custom   = is_custom,
            )

            # Send notice with persistent unlock button
            lock_msg = None
            try:
                view = UnlockView()
                lock_msg = await channel.send("\n".join(lock_parts), view=view)
            except discord.Forbidden:
                print(f"[autopause] _schedule_lock: could not send lock message in #{channel.name}")

            # ── Send lock-log embed to the log channel ────────────────────────
            await self._send_lock_log(
                guild=guild,
                channel=channel,
                pokemon=pokemon,
                ping_type=ping_type,
                trigger_msg=trigger_msg,
                lock_msg=lock_msg,
                unlock_time=unlock_time,
                cfg=cfg,
            )

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

    # ── Lock-log embed ────────────────────────────────────────────────────────

    async def _send_lock_log(
        self,
        *,
        guild:       discord.Guild,
        channel:     discord.TextChannel,
        pokemon:     str,
        ping_type:   str,
        trigger_msg: discord.Message,
        lock_msg,
        unlock_time,
        cfg:         dict,
    ):
        """
        Post a rich embed to the configured log channel whenever a channel is
        locked.  At the top, mentions every user who has `pokemon` in their
        personal ping collection.  Includes jump buttons.
        """
        log_channel_id = cfg.get("log_channel_id")
        if not log_channel_id:
            return   # no log channel configured

        log_channel = guild.get_channel(log_channel_id)
        if log_channel is None:
            print(f"[autopause] _send_lock_log: log channel {log_channel_id} not found")
            return

        # ── Colour / label per type ───────────────────────────────────────────
        type_meta = {
            "rare":     ("🌟 Rare",     discord.Color.red()),
            "regional": ("🗺️ Regional", discord.Color.purple()),
            "custom":   ("⭐ Custom",   discord.Color.gold()),
        }
        type_label, colour = type_meta.get(ping_type, ("❓ Unknown", discord.Color.blurple()))

        # ── Pokémon sprite from PokéAPI (best-effort) ─────────────────────────
        poke_info = pokedata.get(pokemon) or pokedata.get(_get_base_pokemon(pokemon))
        sprite_url = None
        if poke_info:
            dex_num = poke_info.get("dex") or poke_info.get("id")
            if dex_num:
                sprite_url = (
                    f"https://raw.githubusercontent.com/PokeAPI/sprites/master/"
                    f"sprites/pokemon/{dex_num}.png"
                )

        # ── Find users who have this Pokémon in their collection ──────────────
        user_ids = await _find_users_with_pokemon(guild.id, pokemon)
        # Also check base name in case users stored just the base
        base = _get_base_pokemon(pokemon)
        if base.lower() != pokemon.lower():
            base_user_ids = await _find_users_with_pokemon(guild.id, base)
            # Deduplicate while preserving order
            seen = set(user_ids)
            for uid in base_user_ids:
                if uid not in seen:
                    user_ids.append(uid)
                    seen.add(uid)

        # Only keep user_ids that are still members of the guild
        ping_mentions: list[str] = []
        for uid in user_ids:
            member = guild.get_member(uid)
            if member:
                ping_mentions.append(member.mention)

        # ── Build the embed ───────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"🔒 Channel Locked — {pokemon}",
            color=colour,
        )
        embed.add_field(name="Type",    value=type_label,       inline=True)
        embed.add_field(name="Channel", value=channel.mention,  inline=True)
        if unlock_time:
            unlock_ts = int(unlock_time.timestamp())
            embed.add_field(
                name="Auto-unlock",
                value=f"<t:{unlock_ts}:R>",
                inline=True,
            )

        if sprite_url:
            embed.set_thumbnail(url=sprite_url)

        embed.set_footer(text="Locked at")
        embed.timestamp = discord.utils.utcnow()

        # ── Build view with jump buttons ──────────────────────────────────────
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Jump to Spawn",
            style=discord.ButtonStyle.link,
            url=trigger_msg.jump_url,
            emoji="🔍",
        ))
        if lock_msg is not None:
            view.add_item(discord.ui.Button(
                label="Jump to Lock Notice",
                style=discord.ButtonStyle.link,
                url=lock_msg.jump_url,
                emoji="🔒",
            ))

        # ── Compose the message content (pings sit above the embed) ──────────
        content = None
        if ping_mentions:
            ping_str = " ".join(ping_mentions)
            content = f"📣 **{pokemon}** spotted! {ping_str}"

        try:
            await log_channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            print(f"[autopause] _send_lock_log: log sent to #{log_channel.name} "
                  f"(pinged {len(ping_mentions)} user(s))")
        except discord.Forbidden:
            print(f"[autopause] _send_lock_log: Forbidden — cannot send to #{log_channel.name}")
        except Exception as e:
            print(f"[autopause] _send_lock_log: error: {type(e).__name__}: {e}")

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
        log_ch_id = cfg.get("log_channel_id")
        if log_ch_id:
            log_ch = ctx.guild.get_channel(log_ch_id)
            log_ch_str = log_ch.mention if log_ch else f"`{log_ch_id}` *(not found)*"
        else:
            log_ch_str = "*not set*"
        e.add_field(name="Lock-log channel", value=log_ch_str, inline=False)
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

    @autopause.command(name="setlogchannel")
    @commands.has_permissions(manage_guild=True)
    async def ap_setlogchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where lock-log embeds are posted."""
        await _set_cfg(ctx.guild.id, log_channel_id=channel.id)
        await ctx.reply(
            f"✅ Lock-log channel set to {channel.mention}. "
            f"Embeds will be posted there every time a channel is locked."
        )

    @autopause.command(name="removelogchannel")
    @commands.has_permissions(manage_guild=True)
    async def ap_removelogchannel(self, ctx: commands.Context):
        """Remove / disable the lock-log channel."""
        await _set_cfg(ctx.guild.id, log_channel_id=None)
        await ctx.reply("✅ Lock-log channel removed. No logs will be sent.")

    # ── a!pings ───────────────────────────────────────────────────────────────

    @commands.group(name="pings", aliases=["ping", "col"], invoke_without_command=True)
    async def pings(self, ctx: commands.Context):
        """
        Manage your personal Pokémon ping collection.
        You'll be @mentioned in the log channel when a Pokémon you track gets locked.
        """
        await ctx.send_help(ctx.command)

    @pings.command(name="add")
    async def pings_add(self, ctx: commands.Context, *, pokemon_names: str):
        """
        Add one or more Pokémon to your collection.
        Separate multiple names with commas or spaces.
        Example: a!pings add Ralts, Dratini, Larvitar
        """
        # Split on commas first, then whitespace — strip each token
        raw = [n.strip() for part in pokemon_names.split(",") for n in part.split() if n.strip()]
        if not raw:
            await ctx.reply("❌ Please provide at least one Pokémon name.")
            return

        valid   = []
        invalid = []
        for name in raw:
            # Capitalise first letter for canonical matching
            canonical = name.capitalize()
            if pokedata.get(canonical) or pokedata.get(name):
                valid.append(pokedata.get(canonical) and canonical or name)
            else:
                invalid.append(name)

        lines = []
        if valid:
            added = await _add_user_pings(ctx.guild.id, ctx.author.id, valid)
            already = [n for n in valid if n not in added]
            if added:
                lines.append(f"✅ Added: **{', '.join(added)}**")
            if already:
                lines.append(f"ℹ️ Already in collection: **{', '.join(already)}**")
        if invalid:
            lines.append(f"❌ Not found in Pokédex: **{', '.join(invalid)}**")

        await ctx.reply("\n".join(lines) if lines else "Nothing to do.")

    @pings.command(name="remove")
    async def pings_remove(self, ctx: commands.Context, *, pokemon_names: str):
        """
        Remove one or more Pokémon from your collection.
        Example: a!pings remove Dratini, Larvitar
        """
        raw = [n.strip() for part in pokemon_names.split(",") for n in part.split() if n.strip()]
        if not raw:
            await ctx.reply("❌ Please provide at least one Pokémon name.")
            return

        removed = await _remove_user_pings(ctx.guild.id, ctx.author.id, raw)
        not_found = [n for n in raw if n.lower() not in {r.lower() for r in removed}]

        lines = []
        if removed:
            lines.append(f"✅ Removed: **{', '.join(removed)}**")
        if not_found:
            lines.append(f"ℹ️ Not in your collection: **{', '.join(not_found)}**")
        await ctx.reply("\n".join(lines) if lines else "Nothing to do.")

    @pings.command(name="clear")
    async def pings_clear(self, ctx: commands.Context):
        """Clear your entire Pokémon ping collection."""
        count = await _clear_user_pings(ctx.guild.id, ctx.author.id)
        if count:
            await ctx.reply(f"🗑️ Cleared **{count}** Pokémon from your collection.")
        else:
            await ctx.reply("ℹ️ Your collection is already empty.")

    @pings.command(name="list")
    async def pings_list(self, ctx: commands.Context):
        """Show your current Pokémon ping collection."""
        pokemon = await _get_user_pings(ctx.guild.id, ctx.author.id)
        if not pokemon:
            await ctx.reply(
                "📋 Your collection is empty.\n"
                "Use `a!pings add <name>` to add Pokémon you want to be pinged for."
            )
            return

        e = discord.Embed(
            title=f"📋 {ctx.author.display_name}'s Ping Collection",
            description="\n".join(f"• {p}" for p in sorted(pokemon)),
            color=discord.Color.blurple(),
        )
        e.set_footer(text=f"{len(pokemon)} Pokémon • a!pings add/remove/clear to manage")
        e.set_thumbnail(url=ctx.author.display_avatar.url)
        await ctx.reply(embed=e)


    # ── a!locked ──────────────────────────────────────────────────────────────

    @commands.command(name="locked")
    async def locked(self, ctx: commands.Context):
        """Show currently locked channels (rare & regional & custom)."""
        entries = await _get_locked(ctx.guild.id)
        view    = LockedView(self, ctx.guild, entries)

        # Debug: log what we're showing
        rare_count = len([e for e in entries if e["ping_type"] == "rare" and not e.get("is_custom", False)])
        regional_count = len([e for e in entries if e["ping_type"] == "regional" and not e.get("is_custom", False)])
        custom_count = len([e for e in entries if e.get("is_custom", False)])
        print(f"[autopause] a!locked: rare={rare_count}, regional={regional_count}, custom={custom_count}")

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

    # ── a!cl (custom pokemon list) ────────────────────────────────────────────

    @commands.group(name="cl", aliases=["customlist"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def custom_list(self, ctx: commands.Context):
        """Manage custom Pokémon list for autopause."""
        await ctx.send_help(ctx.command)

    @custom_list.command(name="list")
    @commands.has_permissions(manage_guild=True)
    async def cl_list(self, ctx: commands.Context):
        """Show current custom Pokémon list."""
        custom = await _get_custom_list(ctx.guild.id)
        if not custom:
            await ctx.reply("📋 **Custom list is empty.** Use `a!cl add <pokemon>` to add Pokémon.")
            return

        e = discord.Embed(
            title="📋 Custom Pokémon List",
            description="\n".join(f"• {p}" for p in sorted(custom)),
            color=discord.Color.blue(),
        )
        e.set_footer(text=f"{len(custom)} Pokémon")
        await ctx.reply(embed=e)

    @custom_list.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def cl_add(self, ctx: commands.Context, *, pokemon: str):
        """Add a Pokémon to the custom list."""
        pokemon = pokemon.strip()

        # Validate against pokedata
        if not pokedata.get(pokemon):
            await ctx.reply(f"❌ **{pokemon}** not found in Pokédex.")
            return

        added = await _add_to_custom_list(ctx.guild.id, pokemon)
        if added:
            await ctx.reply(f"✅ Added **{pokemon}** to custom list.")
        else:
            await ctx.reply(f"ℹ️ **{pokemon}** is already in the custom list.")

    @custom_list.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def cl_remove(self, ctx: commands.Context, *, pokemon: str):
        """Remove a Pokémon from the custom list."""
        pokemon = pokemon.strip()

        removed = await _remove_from_custom_list(ctx.guild.id, pokemon)
        if removed:
            await ctx.reply(f"✅ Removed **{pokemon}** from custom list.")
        else:
            await ctx.reply(f"❌ **{pokemon}** not found in custom list.")

    @custom_list.command(name="clear")
    @commands.has_permissions(manage_guild=True)
    async def cl_clear(self, ctx: commands.Context):
        """Clear the entire custom Pokémon list."""
        await _clear_custom_list(ctx.guild.id)
        await ctx.reply("🗑️ **Custom list cleared.**")

    # ── Error handler ─────────────────────────────────────────────────────────

    @autopause.error
    async def ap_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(AutopauseCog(bot))
