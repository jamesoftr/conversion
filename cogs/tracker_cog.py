"""
cogs/tracker_cog.py  —  Unified Pokétwo tracker (catches + boxes).

Commands
────────
  a!profile  [@user]          Unified profile: catch stats + box stats, all in one view
  a!pf       [@user]          Alias for a!profile
  a!serverstats               Server-wide combined stats (catches + boxes, today / all-time)
  a!ss                        Alias for a!serverstats
  a!check                     Reply to any Pokétwo message to manually record it
                              (works for catches, flees, AND box openings)
  a!fled-logs <cat> <ch>      Admin: route fled alerts to a channel
  a!fled-logs list            Admin: show current routing
  a!cleardata                 [Owner] Wipe ALL data for this guild (double-confirm)
  a!cleardata @user/id        [Mod] Wipe all data for one user (single-confirm)

Background task
───────────────
  Runs at UTC midnight every day — purges fled docs older than 7 days.
  No other collection is touched.
"""

import re
import asyncio
import datetime
from typing import Optional

import discord
from discord.ext import commands, tasks

import db
import parser as pk_parser
import pokedata
import categories as cats
from categories import get_category, get_category_for_pokemon
from config import E

POKETWO_BOT_ID = 716390085896962058
PAGE_SIZE      = 15
OWNER_ID: int | None = None   # set to your bot owner's user ID, or leave None


# ══════════════════════════════════════════════════════════════════════════════
# ── Helpers ───────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _rank(i: int) -> str:
    return E.rank_emoji(i) if hasattr(E, "rank_emoji") else f"**#{i}**"


# ══════════════════════════════════════════════════════════════════════════════
# ── PROFILE VIEW ──────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class ProfileView(discord.ui.View):
    """
    Main profile view.  Buttons switch the embed between different panels:
      • Catch Stats (default)
      • Box Stats
      • Type Breakdown
      • Region Breakdown
      • Pokémon List (paginated)
    If the invoker is the profile owner (or a mod), a Reset button is shown.
    """

    def __init__(
        self,
        *,
        bot,
        guild_id:      int,
        target:        discord.Member | discord.User,
        invoker:       discord.Member | discord.User,
        stats:         dict,
        stats_alltime: dict,
        box_days:      list[dict],
        poke_list:     list[dict],
        poke_list_all: list[dict],
        reset_unix:    int,
        today_label:   str,
    ):
        super().__init__(timeout=300)
        self.bot           = bot
        self.guild_id      = guild_id
        self.target        = target
        self.invoker       = invoker
        self.stats         = stats
        self.stats_alltime = stats_alltime
        self.box_days      = box_days
        self.poke_list     = poke_list        # today
        self.poke_list_all = poke_list_all    # all time
        self.reset_unix    = reset_unix
        self.today_label   = today_label

        # Show reset button only to the profile owner or mods
        can_reset = (
            invoker.id == target.id
            or (
                isinstance(invoker, discord.Member)
                and invoker.guild_permissions.manage_guild
            )
        )
        if not can_reset:
            self.remove_item(self.reset_btn)

    # ── Catch stats embed (default panel) ─────────────────────────────────────

    def catch_embed(self) -> discord.Embed:
        s  = self.stats
        sa = self.stats_alltime
        shiny_today = s["shiny"]  + s["chain_shiny"]
        shiny_all   = sa["shiny"] + sa["chain_shiny"]

        rows = [
            ("Catches",                s["total"],      sa["total"]),
            (f"{E.shiny} Shiny",       shiny_today,     shiny_all),
            (f"{E.gigantamax}",        s["gigantamax"], sa["gigantamax"]),
            (f"{E.chain_shiny} Chain", s["chain_shiny"],sa["chain_shiny"]),
        ]

        today_col = "\n".join(
            f"{E.reply} **{label}** — **{v}**" for label, v, _ in rows
        )
        alltime_col = "\n".join(
            f"{E.reply} **{label}** — **{v}**" for label, _, v in rows
        )

        embed = discord.Embed(
            title=f"{E.profile} {self.target.display_name}",
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=self.target.display_avatar.url)
        embed.add_field(name=f"📅 Today — {self.today_label}", value=today_col,   inline=True)
        embed.add_field(name="🏅 All Time",                    value=alltime_col, inline=True)
        embed.add_field(
            name="\u200b",
            value=(
                f"> Resets <t:{self.reset_unix}:R> — <t:{self.reset_unix}:t>"
            ),
            inline=False,
        )
        embed.set_footer(text="📦 Box Stats  •  🔬 Type  •  🗺️ Region  •  📋 Pokémon List")
        return embed

    # ── Box stats embed ────────────────────────────────────────────────────────

    def box_summary_embed(self) -> discord.Embed:
        days = self.box_days
        if not days:
            embed = discord.Embed(
                title=f"📦 {self.target.display_name} — Box Stats",
                description="*No box-opening data recorded yet.*",
                color=discord.Color.blurple(),
            )
            embed.set_thumbnail(url=self.target.display_avatar.url)
            return embed

        total_boxes   = sum(d["boxes_opened"]  for d in days)
        total_pokemon = sum(d["total_pokemon"] for d in days)
        total_shinies = sum(len(d["shinies"])  for d in days)
        total_high    = sum(len(d["high_iv"])  for d in days)
        total_low     = sum(len(d["low_iv"])   for d in days)
        total_coins   = sum(d["total_coins"]   for d in days)
        total_shards  = sum(d["total_shards"]  for d in days)
        total_redeems = sum(d.get("total_redeems", 0) for d in days)
        shiny_rate    = (
            f"1 / {total_pokemon // total_shinies}" if total_shinies else "—"
        )

        # Today's slice
        today_str  = self.today_label
        today_days = [d for d in days if d["date"] == today_str]
        t_boxes    = sum(d["boxes_opened"]  for d in today_days)
        t_pokemon  = sum(d["total_pokemon"] for d in today_days)
        t_shinies  = sum(len(d["shinies"])  for d in today_days)
        t_coins    = sum(d["total_coins"]   for d in today_days)
        t_shards   = sum(d["total_shards"]  for d in today_days)
        t_redeems  = sum(d.get("total_redeems", 0) for d in today_days)
        t_shiny_rate = (
            f"1 / {t_pokemon // t_shinies}" if t_shinies else "—"
        )

        today_col = (
            f"{E.reply} 📦 **Boxes** — **{t_boxes}**\n"
            f"{E.reply} 🎴 **Pokémon** — **{t_pokemon}**\n"
            f"{E.reply} ✨ **Shinies** — **{t_shinies}** `({t_shiny_rate})`\n"
            f"{E.reply} 🪙 **Coins** — **{t_coins:,}**\n"
            f"{E.reply} 💎 **Shards** — **{t_shards}**\n"
            f"{E.reply} 🎟️ **Redeems** — **{t_redeems}**"
        )
        alltime_col = (
            f"{E.reply} 📦 **Boxes** — **{total_boxes}**\n"
            f"{E.reply} 🎴 **Pokémon** — **{total_pokemon}**\n"
            f"{E.reply} ✨ **Shinies** — **{total_shinies}** `({shiny_rate})`\n"
            f"{E.reply} 🔺 **High IV ≥90%** — **{total_high}**\n"
            f"{E.reply} 🔻 **Low IV ≤10%** — **{total_low}**\n"
            f"{E.reply} 🪙 **Coins** — **{total_coins:,}**\n"
            f"{E.reply} 💎 **Shards** — **{total_shards}**\n"
            f"{E.reply} 🎟️ **Redeems** — **{total_redeems}**"
        )

        embed = discord.Embed(
            title=f"📦 {self.target.display_name} — Box Stats",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=self.target.display_avatar.url)
        embed.add_field(name=f"📅 Today — {self.today_label}", value=today_col,   inline=True)
        embed.add_field(name="🏅 All Time",                    value=alltime_col, inline=True)
        embed.add_field(
            name="\u200b",
            value=f"> {len(days)} session day(s) tracked total",
            inline=False,
        )
        embed.set_footer(text="Use ◀ ▶ after switching to Day-by-Day to browse sessions")
        return embed

    # ── Button callbacks ───────────────────────────────────────────────────────

    @discord.ui.button(label="Box Stats", emoji="📦", style=discord.ButtonStyle.primary, row=0)
    async def box_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        pages = _build_box_pages(self.target, self.box_days)
        view  = BoxDayNav(pages=pages, parent_view=self)
        await interaction.edit_original_response(embed=pages[0], view=view)

    @discord.ui.button(label="Type Stats", emoji="🔬", style=discord.ButtonStyle.primary, row=0)
    async def type_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        type_totals = pokedata.aggregate_types(self.poke_list)
        if not type_totals:
            await interaction.followup.send("No type data for today.", ephemeral=True)
            return
        lines = [
            f"{E.reply} `{t:<12}` **{c}**"
            for t, c in list(type_totals.items())[:25]
        ]
        embed = self.catch_embed()
        embed.add_field(
            name=f"🔬 Type Breakdown — {self.today_label}",
            value="\n".join(lines),
            inline=False,
        )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Region Stats", emoji="🗺️", style=discord.ButtonStyle.secondary, row=0)
    async def region_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        region_totals = pokedata.aggregate_regions(self.poke_list)
        if not region_totals:
            await interaction.followup.send("No region data for today.", ephemeral=True)
            return
        lines = [
            f"{E.reply} `{r:<14}` **{c}**"
            for r, c in region_totals.items()
        ]
        embed = self.catch_embed()
        embed.add_field(
            name=f"🗺️ Region Breakdown — {self.today_label}",
            value="\n".join(lines),
            inline=False,
        )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Pokémon List", emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def poke_list_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        view = PokeListView(parent=self, poke_list=self.poke_list, mode="today")
        await interaction.edit_original_response(
            embed=view.build_embed(), view=view
        )

    @discord.ui.button(label="Reset Data", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def reset_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        # Only the target or a mod can confirm
        is_mod = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )
        if interaction.user.id != self.target.id and not is_mod:
            await interaction.response.send_message(
                "❌ You can only reset your own data.", ephemeral=True
            )
            return
        view = ResetConfirmView(
            bot=self.bot,
            guild_id=self.guild_id,
            target=self.target,
            requester=interaction.user,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Confirm Data Reset",
                description=(
                    f"This will permanently delete **all catches and box records**"
                    f" for **{self.target.display_name}**.\n\n"
                    f"**This cannot be undone.**"
                ),
                color=discord.Color.orange(),
            ),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="↩ Back", emoji="🔙", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        await interaction.edit_original_response(embed=self.catch_embed(), view=self)


# ── Box day-by-day navigation ─────────────────────────────────────────────────

def _build_box_pages(
    target: discord.Member | discord.User,
    days:   list[dict],
) -> list[discord.Embed]:
    """Page 0 = summary; pages 1+ = one embed per day (newest first)."""
    pages: list[discord.Embed] = []

    if not days:
        e = discord.Embed(
            title=f"📦 {target.display_name} — Box Stats",
            description="*No box-opening data recorded yet.*",
            color=discord.Color.blurple(),
        )
        e.set_thumbnail(url=target.display_avatar.url)
        pages.append(e)
        return pages

    total_boxes   = sum(d["boxes_opened"]  for d in days)
    total_pokemon = sum(d["total_pokemon"] for d in days)
    total_shinies = sum(len(d["shinies"])  for d in days)
    total_high    = sum(len(d["high_iv"])  for d in days)
    total_low     = sum(len(d["low_iv"])   for d in days)
    total_coins   = sum(d["total_coins"]   for d in days)
    total_shards  = sum(d["total_shards"]  for d in days)
    total_redeems = sum(d.get("total_redeems", 0) for d in days)
    shiny_rate    = (
        f"1 / {total_pokemon // total_shinies}" if total_shinies else "—"
    )

    summary = discord.Embed(color=discord.Color.gold())
    summary.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    summary.title = "📦 Box Stats — All Time Summary"
    summary.description = (
        f"> {len(days)} session day(s) tracked\n\n"
        f"- 📦  **Boxes opened** — `{total_boxes}`\n"
        f"- 🎴  **Pokémon unboxed** — `{total_pokemon}`\n"
        f"- ✨  **Shinies** — `{total_shinies}`  `(rate: {shiny_rate})`\n"
        f"- 🔺  **High IV ≥90%** — `{total_high}`\n"
        f"- 🔻  **Low IV ≤10%** — `{total_low}`\n"
        f"- 🪙  **Coins** — `{total_coins:,}`\n"
        f"- 💎  **Shards** — `{total_shards}`\n"
        f"- 🎟️  **Redeems** — `{total_redeems}`"
    )
    pages.append(summary)

    for day in sorted(days, key=lambda d: d["date"], reverse=True):
        try:
            d_obj  = datetime.date.fromisoformat(day["date"])
            label  = d_obj.strftime("%a %d %b %Y")
        except ValueError:
            label = day["date"]

        day_shinies = len(day["shinies"])
        day_shiny_rate = (
            f"1 / {day['total_pokemon'] // day_shinies}"
            if day_shinies else "—"
        )

        shiny_lines   = [f"- ✨  {s['name']} — `{s['iv']:.2f}%`" for s in day["shinies"]]
        high_iv_lines = [f"- 🔺  {h['name']} — `{h['iv']:.2f}%`" for h in day["high_iv"]]
        low_iv_lines  = [f"- 🔻  {l['name']} — `{l['iv']:.2f}%`" for l in day["low_iv"]]
        notable_block = "\n".join(shiny_lines + high_iv_lines + low_iv_lines)

        e = discord.Embed(color=discord.Color.blurple())
        e.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        e.title = f"📅 {label}"
        e.description = (
            f"- 📦  **Boxes** — `{day['boxes_opened']}`\n"
            f"- 🎴  **Pokémon** — `{day['total_pokemon']}`\n"
            f"- ✨  **Shinies** — `{day_shinies}`  `(rate: {day_shiny_rate})`\n"
            f"- 🪙  **Coins** — `{day['total_coins']:,}`\n"
            f"- 💎  **Shards** — `{day['total_shards']}`\n"
            f"- 🎟️  **Redeems** — `{day.get('total_redeems', 0)}`"
            + (
                f"\n\n**Notable pulls**\n{notable_block}"
                if notable_block else
                "\n\n*No notable pulls this day.*"
            )
        )
        pages.append(e)

    return pages


class BoxDayNav(discord.ui.View):
    """Prev / Next navigation for box day pages, with a Back button."""

    def __init__(self, pages: list[discord.Embed], parent_view: "ProfileView"):
        super().__init__(timeout=300)
        self.pages       = pages
        self.page        = 0
        self.parent_view = parent_view
        self._sync()

    def _sync(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        self.page -= 1
        self._sync()
        await interaction.edit_original_response(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        self.page += 1
        self._sync()
        await interaction.edit_original_response(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="↩ Back to Profile", emoji="🔙", style=discord.ButtonStyle.primary)
    async def back_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        await interaction.edit_original_response(
            embed=self.parent_view.catch_embed(),
            view=self.parent_view,
        )


# ── Pokémon list view ─────────────────────────────────────────────────────────

class PokeListView(discord.ui.View):
    """Paginated Pokémon list with Today / All-Time toggle."""

    def __init__(
        self,
        *,
        parent: "ProfileView",
        poke_list: list[dict],
        mode: str = "today",  # "today" | "alltime"
    ):
        super().__init__(timeout=300)
        self.parent    = parent
        self.mode      = mode
        self.page      = 0
        self._sync_list()
        self._sync_buttons()

    def _sync_list(self):
        self.current_list = (
            self.parent.poke_list if self.mode == "today"
            else self.parent.poke_list_all
        )
        self.total_pages = max(
            1, (len(self.current_list) + PAGE_SIZE - 1) // PAGE_SIZE
        )

    def _sync_buttons(self):
        self.prev_btn.disabled   = self.page == 0
        self.next_btn.disabled   = self.page >= self.total_pages - 1
        self.today_btn.disabled  = self.mode == "today"
        self.all_btn.disabled    = self.mode == "alltime"

    def build_embed(self) -> discord.Embed:
        chunk = self.current_list[
            self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE
        ]
        lines = [
            f"{E.reply} `{i + self.page * PAGE_SIZE + 1:>3}.` **{e['pokemon']}** × {e['count']}"
            for i, e in enumerate(chunk)
        ]
        window = "Today" if self.mode == "today" else "All Time"
        embed  = self.parent.catch_embed()
        embed.add_field(
            name=(
                f"📋 Pokémon Caught — {window} "
                f"(page {self.page + 1}/{self.total_pages})"
            ),
            value="\n".join(lines) if lines else "*None yet.*",
            inline=False,
        )
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        self.page -= 1
        self._sync_buttons()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        self.page += 1
        self._sync_buttons()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Today", emoji="📅", style=discord.ButtonStyle.primary, row=1)
    async def today_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        self.mode = "today"
        self.page = 0
        self._sync_list()
        self._sync_buttons()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="All Time", emoji="🏅", style=discord.ButtonStyle.primary, row=1)
    async def all_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        self.mode = "alltime"
        self.page = 0
        self._sync_list()
        self._sync_buttons()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="↩ Back", emoji="🔙", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        await interaction.edit_original_response(
            embed=self.parent.catch_embed(), view=self.parent
        )


# ── Reset confirm view ────────────────────────────────────────────────────────

class ResetConfirmView(discord.ui.View):
    def __init__(self, *, bot, guild_id: int, target, requester):
        super().__init__(timeout=60)
        self.bot       = bot
        self.guild_id  = guild_id
        self.target    = target
        self.requester = requester

    @discord.ui.button(label="Yes, wipe it", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Not your confirm.", ephemeral=True)
            return
        await interaction.response.defer()
        deleted = await db.reset_user_data(self.guild_id, self.target.id)
        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="🗑️ Data Wiped",
                description=(
                    f"{E.reply} **User** — {self.target.mention}\n"
                    f"{E.reply} **Catches deleted** — `{deleted['catches']}`\n"
                    f"{E.reply} **Box records deleted** — `{deleted['box_openings']}`"
                ),
                color=discord.Color.green(),
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Not your confirm.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Cancelled",
                description="No data was deleted.",
                color=discord.Color.greyple(),
            ),
            view=None,
        )


# ══════════════════════════════════════════════════════════════════════════════
# ── SERVER STATS VIEW ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class ServerStatsView(discord.ui.View):
    def __init__(self, *, guild: discord.Guild, reset_unix: int, today_label: str):
        super().__init__(timeout=300)
        self.guild       = guild
        self.reset_unix  = reset_unix
        self.today_label = today_label
        self._window     = "today"
        self.today_btn.disabled  = True   # default selection
        self.alltime_btn.disabled = False

    async def build_embed(self) -> discord.Embed:
        gid = self.guild.id
        if self._window == "today":
            s     = await db.get_server_stats(gid)
            title = f"🌐 {self.guild.name} — Server Stats"
            win   = (
                f"> 📅 **Today — {self.today_label}**\n"
                f"> Resets <t:{self.reset_unix}:R>"
            )
        else:
            s     = await db.get_server_stats_alltime(gid)
            title = f"🌐 {self.guild.name} — Server Stats"
            win   = "> 🏅 **All Time** — complete history"

        shiny_total = s["shiny"] + s["chain_shiny"]

        catch_block = (
            f"{E.reply} 🎯 **Total Catches** — **{s['catches']}**\n"
            f"{E.reply} {E.shiny} **Shiny** — **{shiny_total}**"
            + (f"  `({s['chain_shiny']} chain)`" if s["chain_shiny"] else "") + "\n"
            f"{E.reply} {E.gigantamax} **Gigantamax** — **{s['gigantamax']}**"
        )

        box_block = (
            f"{E.reply} 📦 **Boxes Opened** — **{s['boxes_opened']}**\n"
            f"{E.reply} 🎴 **Pokémon Unboxed** — **{s['total_pokemon']}**\n"
            f"{E.reply} ✨ **Box Shinies** — **{s['box_shinies']}**\n"
            f"{E.reply} 🪙 **Coins** — **{s['total_coins']:,}**\n"
            f"{E.reply} 💎 **Shards** — **{s['total_shards']}**\n"
            f"{E.reply} 🎟️ **Redeems** — **{s['total_redeems']}**"
        )

        embed = discord.Embed(title=title, color=discord.Color.gold())
        embed.set_thumbnail(url=self.guild.icon.url if self.guild.icon else None)
        embed.description = win
        embed.add_field(name="🎯 Catches", value=catch_block, inline=False)
        embed.add_field(name="📦 Box Openings", value=box_block, inline=False)
        return embed

    @discord.ui.button(label="Today", emoji="📅", style=discord.ButtonStyle.primary)
    async def today_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.defer()
        self._window = "today"
        self.today_btn.disabled   = True
        self.alltime_btn.disabled = False
        await interaction.edit_original_response(
            embed=await self.build_embed(), view=self
        )

    @discord.ui.button(label="All Time", emoji="🏅", style=discord.ButtonStyle.secondary)
    async def alltime_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.defer()
        self._window = "alltime"
        self.today_btn.disabled   = False
        self.alltime_btn.disabled = True
        await interaction.edit_original_response(
            embed=await self.build_embed(), view=self
        )


# ══════════════════════════════════════════════════════════════════════════════
# ── CLEARDATA CONFIRM VIEWS ───────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class ClearUserConfirmView(discord.ui.View):
    """Single-confirm: wipe one user's data."""

    def __init__(self, *, guild_id: int, target, requester):
        super().__init__(timeout=60)
        self.guild_id  = guild_id
        self.target    = target
        self.requester = requester

    @discord.ui.button(label="Confirm Wipe", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Not your command.", ephemeral=True)
            return
        await interaction.response.defer()
        deleted = await db.reset_user_data(self.guild_id, self.target.id)
        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="🗑️ User Data Cleared",
                description=(
                    f"{E.reply} **User** — {self.target.mention}\n"
                    f"{E.reply} **Catches deleted** — `{deleted['catches']}`\n"
                    f"{E.reply} **Box records deleted** — `{deleted['box_openings']}`"
                ),
                color=discord.Color.green(),
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Not your command.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Cancelled", description="No data was deleted.",
                color=discord.Color.greyple(),
            ),
            view=None,
        )


class ClearGuildStep1View(discord.ui.View):
    """First confirm for full guild wipe."""

    def __init__(self, *, guild_id: int, requester):
        super().__init__(timeout=60)
        self.guild_id  = guild_id
        self.requester = requester

    @discord.ui.button(label="Yes, continue", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def step1_yes(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Not your command.", ephemeral=True)
            return
        self.stop()
        view = ClearGuildStep2View(guild_id=self.guild_id, requester=self.requester)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⚠️ Final Warning",
                description=(
                    "**This is your last chance.**\n\n"
                    "All catches, box records, flee logs, and dedup caches for this"
                    " server will be **permanently deleted**.\n\n"
                    "Click **WIPE EVERYTHING** to confirm."
                ),
                color=discord.Color.red(),
            ),
            view=view,
        )

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def step1_no(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Not your command.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Cancelled", description="Nothing was deleted.",
                color=discord.Color.greyple(),
            ),
            view=None,
        )


class ClearGuildStep2View(discord.ui.View):
    """Second (final) confirm for full guild wipe."""

    def __init__(self, *, guild_id: int, requester):
        super().__init__(timeout=60)
        self.guild_id  = guild_id
        self.requester = requester

    @discord.ui.button(label="WIPE EVERYTHING", emoji="💥", style=discord.ButtonStyle.danger)
    async def final_confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Not your command.", ephemeral=True)
            return
        await interaction.response.defer()
        deleted = await db.clear_guild_data(self.guild_id)
        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="💥 Guild Data Cleared",
                description=(
                    f"{E.reply} **Catches deleted** — `{deleted['catches']}`\n"
                    f"{E.reply} **Flees deleted** — `{deleted['flees']}`\n"
                    f"{E.reply} **Dedup cache cleared** — `{deleted.get('seen_messages', 0)}`"
                ),
                color=discord.Color.green(),
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Not your command.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Cancelled", description="Nothing was deleted.",
                color=discord.Color.greyple(),
            ),
            view=None,
        )


# ══════════════════════════════════════════════════════════════════════════════
# ── BOX EMBED PARSER (inlined from boxtracker_cog) ───────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

import re as _re

_AVATAR_URL_RE = _re.compile(r"cdn\.discordapp\.com/avatars/(\d+)/", _re.IGNORECASE)
_BOX_TITLE_RE  = _re.compile(r"You open (\d+)\s+📦\s+Supply Crates", _re.IGNORECASE)
_POKEMON_LINE_RE = _re.compile(
    r"\*\*<:[^>]+>\s*(?P<shiny>✨\s*)?Level\s+\d+\s+(?P<name>[^<(]+?)"
    r"(?:<[^>]+>)?\s*\((?P<iv>[\d.]+)%\)\*\*",
    _re.IGNORECASE,
)
_COIN_LINE_RE   = _re.compile(r"([\d,]+)\s+Pokécoins", _re.IGNORECASE)
_SHARD_LINE_RE  = _re.compile(r"(\d+)\s+Shards?",      _re.IGNORECASE)
_REDEEM_LINE_RE = _re.compile(r"(\d+)\s+Redeem",       _re.IGNORECASE)
_HIGH_IV = 90.0
_LOW_IV  = 10.0


async def _resolve_opener_id(message: discord.Message) -> Optional[int]:
    if message.reference:
        try:
            resolved = message.reference.resolved
            if resolved and isinstance(resolved, discord.Message):
                return resolved.author.id
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
            return ref_msg.author.id
        except (discord.NotFound, discord.HTTPException):
            pass
    for embed in message.embeds:
        icon_url = embed.author.icon_url if embed.author else None
        if icon_url:
            m = _AVATAR_URL_RE.search(icon_url)
            if m:
                return int(m.group(1))
    return None


def _parse_box_embed(embed: discord.Embed) -> Optional[dict]:
    title = embed.title or ""
    m = _BOX_TITLE_RE.search(title)
    if not m:
        return None
    boxes_opened  = int(m.group(1))
    desc          = embed.description or ""
    total_pokemon = 0
    shinies:  list[dict] = []
    high_iv:  list[dict] = []
    low_iv:   list[dict] = []
    total_coins   = 0
    total_shards  = 0
    total_redeems = 0

    for line in desc.splitlines():
        line = line.strip()
        pm = _POKEMON_LINE_RE.search(line)
        if pm:
            total_pokemon += 1
            iv    = float(pm.group("iv"))
            name  = pm.group("name").strip()
            entry = {"name": name, "iv": iv}
            if pm.group("shiny"):
                shinies.append(entry)
            if iv >= _HIGH_IV:
                high_iv.append(entry)
            if iv <= _LOW_IV:
                low_iv.append(entry)
            continue
        cm = _COIN_LINE_RE.search(line)
        if cm:
            total_coins += int(cm.group(1).replace(",", ""))
            continue
        sm = _SHARD_LINE_RE.search(line)
        if sm:
            total_shards += int(sm.group(1))
            continue
        rm = _REDEEM_LINE_RE.search(line)
        if rm:
            total_redeems += int(rm.group(1))

    return {
        "boxes_opened":  boxes_opened,
        "total_pokemon": total_pokemon,
        "shinies":       shinies,
        "high_iv":       high_iv,
        "low_iv":        low_iv,
        "total_coins":   total_coins,
        "total_shards":  total_shards,
        "total_redeems": total_redeems,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── MAIN COG ──────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TrackerCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._purge_old_flees.start()

    def cog_unload(self):
        self._purge_old_flees.cancel()

    # ── Nightly fled purge task ───────────────────────────────────────────────

    @tasks.loop(hours=24)
    async def _purge_old_flees(self):
        """Delete fled documents older than 7 days. Runs every 24 h at UTC midnight."""
        deleted = await db.purge_old_flees(days=7)
        if deleted:
            print(f"[tracker] Purged {deleted} old flee record(s).")

    @_purge_old_flees.before_loop
    async def _before_purge(self):
        """Wait until the next UTC midnight before first run."""
        await self.bot.wait_until_ready()
        now    = datetime.datetime.now(datetime.timezone.utc)
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep((midnight - now).total_seconds())

    # ── on_message listener ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != POKETWO_BOT_ID:
            return
        if not message.guild:
            return
        await self._process_poketwo_message(message)

    async def _process_poketwo_message(
        self,
        message: discord.Message,
        date_override: Optional[datetime.date] = None,
    ):
        """
        Try to parse a Pokétwo message as a catch, flee, or box opening.
        Returns ("catch", catch), ("flee", flee), ("box", data), or None.
        """
        guild_id   = message.guild.id
        channel_id = message.channel.id
        full_text  = message.content or ""

        # ── Catch ─────────────────────────────────────────────────────────────
        catch = pk_parser.parse_catch(full_text)
        if catch:
            recorded = await db.record_catch(
                guild_id=guild_id,
                user_id=catch.user_id,
                pokemon=catch.pokemon,
                iv=catch.iv,
                shiny=catch.shiny,
                gigantamax=catch.gigantamax,
                chain_shiny=catch.chain_shiny,
                channel_id=channel_id,
                message_id=message.id,
            )
            if not recorded:
                return None
            return ("catch", catch)

        # ── Flee ──────────────────────────────────────────────────────────────
        for embed in message.embeds:
            flee = pk_parser.parse_flee(embed.title or "")
            if flee:
                await db.record_flee(guild_id, flee.pokemon, channel_id)
                image_url = pokedata.cdn_image_url(flee.pokemon)
                await self._dispatch_fled_logs(message, guild_id, flee.pokemon, image_url)
                return ("flee", flee)

        # ── Box opening ───────────────────────────────────────────────────────
        for embed in message.embeds:
            reward_data = _parse_box_embed(embed)
            if reward_data is None:
                continue
            if not await db.mark_box_message_seen(message.id):
                return "duplicate"
            user_id = await _resolve_opener_id(message)
            if user_id is None:
                return None
            record_date = date_override or message.created_at.date()
            await db.record_box_opening(
                guild_id      = guild_id,
                user_id       = user_id,
                boxes_opened  = reward_data["boxes_opened"],
                total_pokemon = reward_data["total_pokemon"],
                shinies       = reward_data["shinies"],
                high_iv       = reward_data["high_iv"],
                low_iv        = reward_data["low_iv"],
                total_coins   = reward_data["total_coins"],
                total_shards  = reward_data["total_shards"],
                total_redeems = reward_data["total_redeems"],
                date_override = record_date,
            )
            return ("box", {**reward_data, "user_id": user_id})

        return None

    async def _dispatch_fled_logs(
        self,
        original_msg: discord.Message,
        guild_id:     int,
        pokemon:      str,
        image_url:    str | None,
    ):
        cat_keys = get_category_for_pokemon(pokemon)
        if not cat_keys:
            return
        notified: set[int] = set()
        for key in cat_keys:
            ch_id = await db.get_fled_log_channel(guild_id, key)
            if not ch_id or ch_id in notified:
                continue
            notified.add(ch_id)
            channel = self.bot.get_channel(ch_id)
            if not channel:
                continue
            cat = get_category(key)
            e = discord.Embed(
                title=f"🚨 {pokemon} fled!",
                description=(
                    f"{E.reply} **Category:** {cat['name']}\n"
                    f"{E.reply} **Spotted in:** {original_msg.channel.mention}"
                ),
                color=discord.Color.red(),
                timestamp=original_msg.created_at,
            )
            if image_url:
                e.set_image(url=image_url)
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Jump to Message",
                style=discord.ButtonStyle.link,
                url=original_msg.jump_url,
                emoji="🔗",
            ))
            try:
                await channel.send(embed=e, view=view)
            except discord.Forbidden:
                pass

    # ── a!profile ─────────────────────────────────────────────────────────────

    @commands.command(name="profile", aliases=["pf"])
    async def profile(self, ctx: commands.Context, member: discord.Member = None):
        """
        Unified catch + box profile.

        Usage:
          a!profile          — your own profile
          a!profile @user    — another member's profile
        """
        target   = member or ctx.author
        guild_id = ctx.guild.id

        (
            stats,
            stats_alltime,
            poke_list,
            poke_list_all,
            box_days,
            reset_info,
        ) = await asyncio.gather(
            db.get_user_stats(guild_id, target.id),
            db.get_user_stats_alltime(guild_id, target.id),
            db.get_user_pokemon_list(guild_id, target.id),
            db.get_user_pokemon_list_alltime(guild_id, target.id),
            db.get_box_stats(guild_id, target.id),
            db.get_window_reset_info(guild_id),
        )

        if stats_alltime["total"] == 0 and not box_days:
            await ctx.reply(
                f"No data recorded for **{target.display_name}** yet.",
                mention_author=False,
            )
            return

        view = ProfileView(
            bot           = self.bot,
            guild_id      = guild_id,
            target        = target,
            invoker       = ctx.author,
            stats         = stats,
            stats_alltime = stats_alltime,
            box_days      = box_days,
            poke_list     = poke_list,
            poke_list_all = poke_list_all,
            reset_unix    = reset_info["reset_unix"],
            today_label   = reset_info["today_label"],
        )
        await ctx.reply(embed=view.catch_embed(), view=view, mention_author=False)

    # ── a!serverstats ─────────────────────────────────────────────────────────

    @commands.command(name="serverstats", aliases=["ss"])
    async def serverstats(self, ctx: commands.Context):
        """
        Show combined server-wide stats (catches + box openings).

        Usage:
          a!serverstats
          a!ss
        """
        reset_info = await db.get_window_reset_info(ctx.guild.id)
        view = ServerStatsView(
            guild       = ctx.guild,
            reset_unix  = reset_info["reset_unix"],
            today_label = reset_info["today_label"],
        )
        await ctx.reply(
            embed=await view.build_embed(),
            view=view,
            mention_author=False,
        )

    # ── a!check (unified) ─────────────────────────────────────────────────────

    @commands.command(name="check")
    @commands.has_permissions(manage_guild=True)
    async def check(self, ctx: commands.Context):
        """
        Manually record any Pokétwo message (catch, flee, or box opening).
        Reply to the target Pokétwo message with this command.

        Usage:
          a!check    (as a reply to a Pokétwo message)
        """
        if ctx.message.reference is None:
            await ctx.reply(
                "❌ Please **reply** to the Pokétwo message you want to record.",
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
                f"❌ That message is not from Pokétwo (ID `{POKETWO_BOT_ID}`).",
                mention_author=False,
            )
            return
        if not target.guild:
            await ctx.reply("❌ That message is not in a server.", mention_author=False)
            return

        original_date = target.created_at.date()
        result = await self._process_poketwo_message(target, date_override=original_date)

        if result == "duplicate":
            await ctx.reply("⚠️ Already recorded — skipping.", mention_author=False)
            return

        if result is None:
            await ctx.reply(
                "❌ Could not parse that message as a catch, flee, or box opening.",
                mention_author=False,
            )
            return

        event_type, event = result

        if event_type == "catch":
            flags = []
            if event.shiny:       flags.append(f"{E.shiny} Shiny")
            if event.gigantamax:  flags.append(f"{E.gigantamax} Gigantamax")
            if event.chain_shiny: flags.append(f"{E.chain_shiny} Chain Shiny")
            iv_str = f"{event.iv:.2f}%" if event.iv is not None else "Hidden"
            e = discord.Embed(
                title="✅ Catch recorded",
                description=(
                    f"{E.reply} **Pokémon** — {event.pokemon}\n"
                    f"{E.reply} **User** — <@{event.user_id}>\n"
                    f"{E.reply} **IV** — {iv_str}"
                    + (f"\n{E.reply} " + "  ".join(flags) if flags else "")
                ),
                color=discord.Color.green(),
            )
            e.set_footer(text=f"Recorded by {ctx.author.display_name}")
            await ctx.reply(embed=e, mention_author=False)

        elif event_type == "flee":
            e = discord.Embed(
                title="✅ Flee recorded",
                description=f"{E.reply} **Pokémon** — {event.pokemon}",
                color=discord.Color.orange(),
            )
            e.set_footer(text=f"Recorded by {ctx.author.display_name}")
            await ctx.reply(embed=e, mention_author=False)

        else:  # box
            data = event
            shiny_lines   = [f"- ✨  {s['name']} — `{s['iv']:.2f}%`" for s in data["shinies"]]
            high_iv_lines = [f"- 🔺  {h['name']} — `{h['iv']:.2f}%`" for h in data["high_iv"]]
            low_iv_lines  = [f"- 🔻  {l['name']} — `{l['iv']:.2f}%`" for l in data["low_iv"]]
            notable = "\n".join(shiny_lines + high_iv_lines + low_iv_lines)

            e = discord.Embed(color=discord.Color.green())
            e.title = "✅ Box opening recorded"
            e.description = (
                f"> {original_date.strftime('%a %d %b %Y')}\n\n"
                f"- 👤  **User** — <@{data['user_id']}>\n"
                f"- 📦  **Boxes** — `{data['boxes_opened']}`\n"
                f"- 🎴  **Pokémon** — `{data['total_pokemon']}`\n"
                f"- 🪙  **Coins** — `{data['total_coins']:,}`\n"
                f"- 💎  **Shards** — `{data['total_shards']}`\n"
                f"- 🎟️  **Redeems** — `{data['total_redeems']}`"
                + (f"\n\n**Notable pulls**\n{notable}" if notable else "")
            )
            e.set_footer(text=f"Recorded by {ctx.author.display_name}")
            await ctx.reply(embed=e, mention_author=False)

    # ── a!fled-logs ───────────────────────────────────────────────────────────

    @commands.command(name="fled-logs")
    @commands.has_permissions(manage_guild=True)
    async def fled_logs(
        self, ctx: commands.Context, category: str = None, channel_id: str = None
    ):
        """
        Configure where fled-log alerts are sent.

        Usage:
          a!fled-logs list
          a!fled-logs <category> <channel_id>
        """
        if not category:
            await ctx.reply(
                "Usage: `a!fled-logs <category> <channel_id>`  or  `a!fled-logs list`\n"
                f"Available categories: `{'`, `'.join(cats.all_keys())}`",
                mention_author=False,
            )
            return

        if category.lower() == "list":
            configs = await db.get_fled_log_channels(ctx.guild.id)
            if not configs:
                await ctx.reply("No fled-log channels configured yet.", mention_author=False)
                return
            lines = [
                f"{E.reply} **{cfg['category_key']}** → "
                + (
                    self.bot.get_channel(cfg["channel_id"]).mention
                    if self.bot.get_channel(cfg["channel_id"])
                    else f"`{cfg['channel_id']}`"
                )
                for cfg in configs
            ]
            await ctx.reply(
                embed=discord.Embed(
                    title="Fled-log routing",
                    description="\n".join(lines),
                    color=discord.Color.blurple(),
                ),
                mention_author=False,
            )
            return

        cat = get_category(category)
        if not cat:
            await ctx.reply(
                f"❌ Unknown category `{category}`.\n"
                f"Available: `{'`, `'.join(cats.all_keys())}`",
                mention_author=False,
            )
            return

        if not channel_id:
            await ctx.reply("❌ Please provide a channel ID or mention.", mention_author=False)
            return

        raw_id = re.sub(r"[<#>]", "", channel_id.strip())
        if not raw_id.isdigit():
            await ctx.reply("❌ Invalid channel ID or mention.", mention_author=False)
            return

        ch_id = int(raw_id)
        await db.set_fled_log_channel(ctx.guild.id, cat["key"], ch_id)
        ch = self.bot.get_channel(ch_id)
        await ctx.reply(
            f"✅ **{cat['name']}** fled alerts → {ch.mention if ch else f'`{ch_id}`'}",
            mention_author=False,
        )

    # ── a!cleardata ───────────────────────────────────────────────────────────

    @commands.command(name="cleardata")
    async def cleardata(
        self,
        ctx: commands.Context,
        target: discord.Member | discord.User = None,
    ):
        """
        Wipe data for a user or for the whole server.

        Usage:
          a!cleardata            — [Owner] wipe ALL data for this guild (double-confirm)
          a!cleardata @user      — [Mod] wipe all data for one user (single-confirm)
          a!cleardata <user_id>  — [Mod] same, by ID
        """
        guild_id = ctx.guild.id

        # ── Single-user wipe (mod permission) ─────────────────────────────────
        if target is not None:
            if not ctx.author.guild_permissions.manage_guild:
                await ctx.reply(
                    "❌ You need **Manage Server** permission to clear a user's data.",
                    mention_author=False,
                )
                return

            view = ClearUserConfirmView(
                guild_id  = guild_id,
                target    = target,
                requester = ctx.author,
            )
            await ctx.reply(
                embed=discord.Embed(
                    title="⚠️ Confirm User Data Wipe",
                    description=(
                        f"This will permanently delete **all catches and box records**"
                        f" for **{target.mention}**.\n\n"
                        f"**This cannot be undone.**"
                    ),
                    color=discord.Color.orange(),
                ),
                view=view,
                mention_author=False,
            )
            return

        # ── Full guild wipe (owner only) ──────────────────────────────────────
        owner_id = OWNER_ID or (await self.bot.application_info()).owner.id
        if ctx.author.id != owner_id:
            await ctx.reply(
                "❌ Only the **bot owner** can wipe the entire server's data.\n"
                "To clear a single user's data, run `a!cleardata @user`.",
                mention_author=False,
            )
            return

        view = ClearGuildStep1View(guild_id=guild_id, requester=ctx.author)
        await ctx.reply(
            embed=discord.Embed(
                title="⚠️ Confirm Guild Data Wipe — Step 1 of 2",
                description=(
                    "You are about to **permanently delete ALL data** for this server:\n\n"
                    "- All catches\n- All box-opening records\n- All flee logs\n"
                    "- All dedup caches\n\n"
                    "**This cannot be undone.** Are you sure you want to continue?"
                ),
                color=discord.Color.orange(),
            ),
            view=view,
            mention_author=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TrackerCog(bot))
