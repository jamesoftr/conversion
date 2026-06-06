"""
cogs/boxtracker_cog.py  —  Pokétwo box-opening tracker.

User ID resolution order (most reliable → least):
  1. Reply reference  — Pokétwo always replies to the user who ran /open,
                        so the referenced message's author IS the opener.
  2. Embed author icon URL — CDN URL contains the opener's user ID when they
                             have an avatar set.
  Both methods are tried; if neither works the message is silently skipped.

Tracked per opening
───────────────────
  • boxes_opened          — how many crates per embed (e.g. 15)
  • total_pokemon         — count of Pokémon slots (for shiny-rate math)
  • shinies               — list of {name, iv}
  • high_iv               — list of {name, iv}   (iv >= 90)
  • low_iv                — list of {name, iv}   (iv <= 10)
  • total_coins           — sum of all Pokécoin rewards
  • total_shards          — sum of all Shard rewards

Commands
────────
a!boxstats [@user]   — view stats ordered by date, with totals
a!boxcheck           — reply to a box opening message to manually record it
"""

import re
import datetime
from typing import Optional

import discord
from discord.ext import commands

import db
from config import E

# ── Constants ─────────────────────────────────────────────────────────────────

POKETWO_BOT_ID = 716390085896962058

# Regex to pull a Discord user ID out of the CDN avatar URL in the embed author icon.
# Works when the opener has a custom avatar set.
# e.g. https://cdn.discordapp.com/avatars/757852191338922025/hash.png?size=1024
AVATAR_URL_RE = re.compile(
    r"cdn\.discordapp\.com/avatars/(\d+)/",
    re.IGNORECASE,
)

# A supply-crate embed title always matches "You open N 📦 Supply Crates..."
BOX_TITLE_RE = re.compile(
    r"You open (\d+)\s+📦\s+Supply Crates",
    re.IGNORECASE,
)

# Each description line is one reward.
# Pokémon line:   **<:_:ID> ✨ Level N Name<gender> (IV%)**   ← shiny has ✨
# Pokémon line:   **<:_:ID> Level N Name<gender> (IV%)**      ← normal
# Coin line:      <:pokecoins:ID> 1,299 Pokécoins
# Shard line:     <:shards:ID> 5 Shards
POKEMON_LINE_RE = re.compile(
    r"\*\*"                           # bold open
    r"<:[^>]+>\s*"                    # sprite emote
    r"(?P<shiny>✨\s*)?"              # optional shiny sparkle
    r"Level\s+\d+\s+"                # level
    r"(?P<name>[^<(]+?)"             # Pokémon name (lazy, stop before gender/iv)
    r"(?:<[^>]+>)?\s*"               # optional gender emote
    r"\((?P<iv>[\d.]+)%\)"           # IV in parens
    r"\*\*",                          # bold close
    re.IGNORECASE,
)
COIN_LINE_RE  = re.compile(r"([\d,]+)\s+Pokécoins", re.IGNORECASE)
SHARD_LINE_RE = re.compile(r"(\d+)\s+Shards?",       re.IGNORECASE)

HIGH_IV_THRESHOLD = 90.0
LOW_IV_THRESHOLD  = 10.0


# ── User ID resolution ────────────────────────────────────────────────────────

async def _resolve_opener_id(message: discord.Message) -> Optional[int]:
    """
    Determine who opened the box. Two strategies tried in order:

    1. Reply reference (preferred)
       Pokétwo always replies to the /open command message, so the author of
       the referenced message is definitively the opener. Works regardless of
       whether the user has an avatar.

    2. Embed author icon URL (fallback)
       When the referenced message is deleted / uncached and can't be fetched,
       we fall back to extracting the user ID from the CDN avatar URL embedded
       in the embed's author icon. This fails for users with no custom avatar
       (Discord uses a default image whose URL doesn't contain a user ID).

    Returns the opener's user ID as an int, or None if both methods fail.
    """
    # ── Strategy 1: reply reference ───────────────────────────────────────────
    if message.reference:
        try:
            # Use the already-resolved message object if Discord cached it
            if message.reference.resolved and isinstance(
                message.reference.resolved, discord.Message
            ):
                return message.reference.resolved.author.id

            # Otherwise fetch it from the API
            ref_msg = await message.channel.fetch_message(
                message.reference.message_id
            )
            return ref_msg.author.id

        except (discord.NotFound, discord.HTTPException):
            # Message was deleted or API call failed — fall through to strategy 2
            pass

    # ── Strategy 2: embed author icon URL ────────────────────────────────────
    for embed in message.embeds:
        icon_url = embed.author.icon_url if embed.author else None
        if not icon_url:
            continue
        m = AVATAR_URL_RE.search(icon_url)
        if m:
            return int(m.group(1))

    return None


# ── Embed parser ──────────────────────────────────────────────────────────────

def _parse_box_embed(embed: discord.Embed) -> Optional[dict]:
    """
    Parse a single embed for box-opening rewards.
    Does NOT resolve the user ID (handled separately by _resolve_opener_id).

    Returns a dict on success, None if the embed is not a box opening:
    {
        "boxes_opened":  int,
        "total_pokemon": int,
        "shinies":       [{"name": str, "iv": float}],
        "high_iv":       [{"name": str, "iv": float}],
        "low_iv":        [{"name": str, "iv": float}],
        "total_coins":   int,
        "total_shards":  int,
    }
    """
    # Title must match
    title = embed.title or ""
    m = BOX_TITLE_RE.search(title)
    if not m:
        return None
    boxes_opened = int(m.group(1))

    # Parse description rewards
    description = embed.description or ""
    total_pokemon = 0
    shinies:  list[dict] = []
    high_iv:  list[dict] = []
    low_iv:   list[dict] = []
    total_coins  = 0
    total_shards = 0

    for line in description.splitlines():
        line = line.strip()

        pm = POKEMON_LINE_RE.search(line)
        if pm:
            total_pokemon += 1
            iv   = float(pm.group("iv"))
            name = pm.group("name").strip()
            entry = {"name": name, "iv": iv}
            if pm.group("shiny"):
                shinies.append(entry)
            if iv >= HIGH_IV_THRESHOLD:
                high_iv.append(entry)
            if iv <= LOW_IV_THRESHOLD:
                low_iv.append(entry)
            continue

        cm = COIN_LINE_RE.search(line)
        if cm:
            total_coins += int(cm.group(1).replace(",", ""))
            continue

        sm = SHARD_LINE_RE.search(line)
        if sm:
            total_shards += int(sm.group(1))

    return {
        "boxes_opened":  boxes_opened,
        "total_pokemon": total_pokemon,
        "shinies":       shinies,
        "high_iv":       high_iv,
        "low_iv":        low_iv,
        "total_coins":   total_coins,
        "total_shards":  total_shards,
    }


# ── DB helpers (thin wrappers — add these to your db.py) ─────────────────────
#
#   db.record_box_opening(guild_id, user_id, *, boxes_opened, total_pokemon,
#                         shinies, high_iv, low_iv, total_coins, total_shards,
#                         date_override=None)
#   db.get_box_stats(guild_id, user_id)   → list of day-docs sorted by date
#
# See the "DB Schema" section at the bottom of this file.


# ── Stats embed builder ───────────────────────────────────────────────────────

def _day_label(date_str: str) -> str:
    """'2026-06-05' → 'Thu 05 Jun 2026'"""
    try:
        d = datetime.date.fromisoformat(date_str)
        return d.strftime("%a %d %b %Y")
    except ValueError:
        return date_str


def _build_stats_pages(
    target: discord.Member | discord.User,
    days: list[dict],
) -> list[discord.Embed]:
    """
    Build a list of embeds:
      page 0  — totals summary
      pages 1+— one embed per day (most recent first)
    """
    pages: list[discord.Embed] = []

    # ── Aggregate totals ──────────────────────────────────────────────────────
    total_boxes   = sum(d["boxes_opened"]  for d in days)
    total_pokemon = sum(d["total_pokemon"] for d in days)
    total_shinies = sum(len(d["shinies"])  for d in days)
    total_high    = sum(len(d["high_iv"])  for d in days)
    total_low     = sum(len(d["low_iv"])   for d in days)
    total_coins   = sum(d["total_coins"]   for d in days)
    total_shards  = sum(d["total_shards"]  for d in days)
    shiny_rate    = (
        f"1 / {total_pokemon // total_shinies}" if total_shinies else "—"
    )

    # ── Summary page ─────────────────────────────────────────────────────────
    summary = discord.Embed(color=discord.Color.gold())
    summary.set_author(
        name=target.display_name,
        icon_url=target.display_avatar.url,
    )
    summary.description = (
        f"# 📦 Box Stats\n"
        f"> {len(days)} session day(s) tracked\n"
        f"\n"
        f"- 📦  **Boxes opened** — `{total_boxes}`\n"
        f"- 🎴  **Pokémon unboxed** — `{total_pokemon}`\n"
        f"- ✨  **Shinies** — `{total_shinies}`\n"
        f"> shiny rate  `{shiny_rate}`\n"
        f"- 🔺  **High IV ≥90%** — `{total_high}`\n"
        f"- 🔻  **Low IV ≤10%** — `{total_low}`\n"
        f"- 🪙  **Coins** — `{total_coins:,}`\n"
        f"- 💎  **Shards** — `{total_shards}`"
    )
    pages.append(summary)

    # ── One embed per day (newest first) ─────────────────────────────────────
    for day in sorted(days, key=lambda d: d["date"], reverse=True):
        label = _day_label(day["date"])
        day_shiny_rate = (
            f"1 / {day['total_pokemon'] // len(day['shinies'])}"
            if day["shinies"] else "—"
        )

        shiny_lines   = [f"- ✨  {s['name']} — `{s['iv']:.2f}%`" for s in day["shinies"]]
        high_iv_lines = [f"- 🔺  {h['name']} — `{h['iv']:.2f}%`" for h in day["high_iv"]]
        low_iv_lines  = [f"- 🔻  {l['name']} — `{l['iv']:.2f}%`" for l in day["low_iv"]]
        notable_block = "\n".join(shiny_lines + high_iv_lines + low_iv_lines)

        e = discord.Embed(color=discord.Color.blurple())
        e.set_author(
            name=target.display_name,
            icon_url=target.display_avatar.url,
        )
        e.description = (
            f"# 📅 {label}\n"
            f"\n"
            f"- 📦  **Boxes** — `{day['boxes_opened']}`\n"
            f"- 🎴  **Pokémon** — `{day['total_pokemon']}`\n"
            f"- 🪙  **Coins** — `{day['total_coins']:,}`\n"
            f"- 💎  **Shards** — `{day['total_shards']}`\n"
            f"> shiny rate  `{day_shiny_rate}`\n"
            + (
                f"\n**Notable pulls**\n{notable_block}"
                if notable_block else
                "\n> *No notable pulls this day*"
            )
        )
        pages.append(e)

    return pages


# ── Paginated view ────────────────────────────────────────────────────────────

class BoxStatsView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page  = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        self.page -= 1
        self._update_buttons()
        await interaction.edit_original_response(
            embed=self.pages[self.page], view=self
        )

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        self.page += 1
        self._update_buttons()
        await interaction.edit_original_response(
            embed=self.pages[self.page], view=self
        )


# ── Main Cog ──────────────────────────────────────────────────────────────────

class BoxTrackerCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Listener ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != POKETWO_BOT_ID:
            return
        if not message.guild:
            return
        await self._process_box_message(message)

    async def _process_box_message(
        self,
        message: discord.Message,
        date_override: Optional[datetime.date] = None,
    ) -> Optional[dict]:
        """
        Resolve the opener, parse the embed rewards, and persist to DB.

        Resolution order:
          1. Reply reference author  (always works, even without an avatar)
          2. Embed author icon URL   (fallback; fails for default-avatar users)

        Returns the full data dict (including resolved user_id) on success,
        or None if the message isn't a box opening or the opener can't be found.
        """
        # Only act on box-opening embeds
        reward_data = None
        for embed in message.embeds:
            reward_data = _parse_box_embed(embed)
            if reward_data is not None:
                break

        if reward_data is None:
            return None

        # Resolve who opened the box
        user_id = await _resolve_opener_id(message)
        if user_id is None:
            return None

        record_date = date_override or message.created_at.date()

        await db.record_box_opening(
            guild_id      = message.guild.id,
            user_id       = user_id,
            boxes_opened  = reward_data["boxes_opened"],
            total_pokemon = reward_data["total_pokemon"],
            shinies       = reward_data["shinies"],
            high_iv       = reward_data["high_iv"],
            low_iv        = reward_data["low_iv"],
            total_coins   = reward_data["total_coins"],
            total_shards  = reward_data["total_shards"],
            date_override = record_date,
        )

        return {**reward_data, "user_id": user_id}

    # ── a!boxstats ────────────────────────────────────────────────────────────

    @commands.command(name="boxstats")
    async def boxstats(
        self,
        ctx: commands.Context,
        target: discord.Member | discord.User = None,
    ):
        """
        View box-opening stats for yourself or another user.

        Usage:
          a!boxstats
          a!boxstats @user
        """
        target = target or ctx.author
        days   = await db.get_box_stats(ctx.guild.id, target.id)

        if not days:
            await ctx.reply(
                f"No box-opening data recorded for **{target.display_name}** yet.",
                mention_author=False,
            )
            return

        pages = _build_stats_pages(target, days)
        view  = BoxStatsView(pages)

        await ctx.reply(embed=pages[0], view=view, mention_author=False)

    # ── a!boxcheck ────────────────────────────────────────────────────────────

    @commands.command(name="boxcheck")
    @commands.has_permissions(manage_guild=True)
    async def boxcheck(self, ctx: commands.Context):
        """
        Manually record a box-opening message.
        Reply to the Pokétwo box-result message with this command.
        The date used is the date of the *original* message, not today.
        """
        if ctx.message.reference is None:
            await ctx.reply(
                "❌ Please **reply** to a Pokétwo box-opening message to record it.",
                mention_author=False,
            )
            return

        try:
            ref    = ctx.message.reference
            target = ref.resolved or await ctx.channel.fetch_message(ref.message_id)
        except discord.NotFound:
            await ctx.reply("❌ Could not fetch that message.", mention_author=False)
            return

        if target.author.id != POKETWO_BOT_ID:
            await ctx.reply(
                f"❌ That message is not from Pokétwo (ID `{POKETWO_BOT_ID}`). "
                "Only Pokétwo box-opening messages can be recorded.",
                mention_author=False,
            )
            return

        if not target.guild:
            await ctx.reply("❌ That message is not in a server.", mention_author=False)
            return

        # Pass the original message's date so historical adds land on the right day
        original_date = target.created_at.date()
        data = await self._process_box_message(target, date_override=original_date)

        if data is None:
            await ctx.reply(
                "❌ Could not parse that message as a box opening.\n"
                "-# Make sure it is a Pokétwo 📦 Supply Crates result embed.",
                mention_author=False,
            )
            return

        shiny_lines   = [f"- ✨  {s['name']} — `{s['iv']:.2f}%`" for s in data["shinies"]]
        high_iv_lines = [f"- 🔺  {h['name']} — `{h['iv']:.2f}%`" for h in data["high_iv"]]
        low_iv_lines  = [f"- 🔻  {l['name']} — `{l['iv']:.2f}%`" for l in data["low_iv"]]
        notable_block = "\n".join(shiny_lines + high_iv_lines + low_iv_lines)

        e = discord.Embed(color=discord.Color.green())
        e.description = (
            f"# ✅ Box opening recorded\n"
            f"> {_day_label(original_date.isoformat())}\n"
            f"\n"
            f"- 👤  **User** — <@{data['user_id']}>\n"
            f"- 📦  **Boxes** — `{data['boxes_opened']}`\n"
            f"- 🎴  **Pokémon** — `{data['total_pokemon']}`\n"
            f"- 🪙  **Coins** — `{data['total_coins']:,}`\n"
            f"- 💎  **Shards** — `{data['total_shards']}`"
            + (
                f"\n\n**Notable pulls**\n{notable_block}"
                if notable_block else ""
            )
        )
        e.set_footer(text=f"Recorded by {ctx.author.display_name}")
        await ctx.reply(embed=e, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(BoxTrackerCog(bot))


# ══════════════════════════════════════════════════════════════════════════════
# DB SCHEMA  (add these functions to your db.py)
# ══════════════════════════════════════════════════════════════════════════════
#
# Collection:  box_openings
# Document structure (one doc per user per day):
#
# {
#   "guild_id":      int,
#   "user_id":       int,
#   "date":          str,          # "YYYY-MM-DD" (UTC)
#   "boxes_opened":  int,
#   "total_pokemon": int,
#   "total_coins":   int,
#   "total_shards":  int,
#   "shinies":  [{"name": str, "iv": float}, ...],
#   "high_iv":  [{"name": str, "iv": float}, ...],
#   "low_iv":   [{"name": str, "iv": float}, ...],
# }
#
# Suggested index:  { guild_id: 1, user_id: 1, date: 1 }  (unique)
#
# ── record_box_opening ────────────────────────────────────────────────────────
#
# async def record_box_opening(
#     guild_id, user_id,
#     boxes_opened, total_pokemon,
#     shinies, high_iv, low_iv,
#     total_coins, total_shards,
#     date_override=None,
# ):
#     coll = _db().box_openings
#     date_str = (date_override or datetime.date.today()).isoformat()
#
#     await coll.update_one(
#         {"guild_id": guild_id, "user_id": user_id, "date": date_str},
#         {"$inc": {
#             "boxes_opened":  boxes_opened,
#             "total_pokemon": total_pokemon,
#             "total_coins":   total_coins,
#             "total_shards":  total_shards,
#         },
#          "$push": {
#             "shinies":  {"$each": shinies},
#             "high_iv":  {"$each": high_iv},
#             "low_iv":   {"$each": low_iv},
#         }},
#         upsert=True,
#     )
#
# ── get_box_stats ─────────────────────────────────────────────────────────────
#
# async def get_box_stats(guild_id, user_id) -> list[dict]:
#     coll   = _db().box_openings
#     cursor = coll.find(
#         {"guild_id": guild_id, "user_id": user_id},
#         sort=[("date", 1)],
#     )
#     docs = await cursor.to_list(length=None)
#     for d in docs:
#         d.setdefault("shinies",  [])
#         d.setdefault("high_iv",  [])
#         d.setdefault("low_iv",   [])
#     return docs
