"""
cogs/tracker_cog.py  —  Pokétwo catch & flee tracker.

Profile shows both last-24h and all-time stats with a clean embed layout,
reply emoji, and a dynamic Discord timestamp for the window reset.

Commands
────────
a!profile [@user]                      — Catch profile (24 h + all-time)
a!check                                — Reply to a Pokétwo msg to manually record it
a!fled-logs <category> <channel_id>   — Admin: route fled alerts to a channel
a!fled-logs list                       — Admin: show current routing
"""

import re
import time
import discord
from discord.ext import commands

import db
import parser as pk_parser
import pokedata
import categories as cats
from categories import get_category, get_category_for_pokemon
from config import E

POKETWO_BOT_ID = 716390085896962058
PAGE_SIZE      = 15
OWNER_ID       = None


def _reset_unix(resets_in_h: float) -> int:
    return int(time.time() + resets_in_h * 3600)


# ── Profile embed builder ─────────────────────────────────────────────────────

def _profile_embed(
    target:        discord.Member | discord.User,
    s:             dict,    # 24h stats
    sa:            dict,    # all-time stats
    reset_unix:    int,
) -> discord.Embed:
    shiny_24h = s["shiny"]  + s["chain_shiny"]
    shiny_all = sa["shiny"] + sa["chain_shiny"]

    # Stat rows: (label, 24h value, all-time value)
    rows = [
        ("Catches",          s["total"],       sa["total"]),
        (f"{E.shiny} Shiny", shiny_24h,        shiny_all),
        (f"{E.gigantamax}",  s["gigantamax"],  sa["gigantamax"]),
        (f"{E.chain_shiny} Chain", s["chain_shiny"], sa["chain_shiny"]),
    ]

    # Left column: 24h
    col_24h = "\n".join(
        f"{E.reply} **{label}** — **{v24}**"
        for label, v24, _ in rows
    )
    # Right column: all-time
    col_all = "\n".join(
        f"{E.reply} **{label}** — **{vall}**"
        for label, _, vall in rows
    )

    embed = discord.Embed(
        title=f"{E.profile} {target.display_name}",
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    embed.add_field(name="📅 Last 24 Hours", value=col_24h, inline=True)
    embed.add_field(name="🏅 All Time",       value=col_all, inline=True)
    embed.add_field(
        name="\u200b",
        value=(
            f"> 24h window resets <t:{reset_unix}:R>\n"
            f"> <t:{reset_unix}:F>"
        ),
        inline=False,
    )
    return embed


# ── Profile View (buttons) ────────────────────────────────────────────────────

class ProfileView(discord.ui.View):
    def __init__(
        self,
        guild_id:      int,
        target:        discord.Member | discord.User,
        stats:         dict,
        stats_alltime: dict,
        poke_list:     list[dict],
        reset_unix:    int,
    ):
        super().__init__(timeout=300)
        self.guild_id      = guild_id
        self.target        = target
        self.stats         = stats
        self.stats_alltime = stats_alltime
        self.poke_list     = poke_list
        self.reset_unix    = reset_unix

    def _base_embed(self) -> discord.Embed:
        return _profile_embed(self.target, self.stats, self.stats_alltime, self.reset_unix)

    @discord.ui.button(label="Type Stats", emoji="🔬", style=discord.ButtonStyle.primary)
    async def type_stats_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        type_totals = pokedata.aggregate_types(self.poke_list)
        if not type_totals:
            await interaction.followup.send("No type data for the last 24 hours.", ephemeral=True)
            return
        lines = [
            f"{E.reply} `{t:<12}` **{c}**"
            for t, c in list(type_totals.items())[:25]
        ]
        e = self._base_embed()
        e.add_field(
            name="🔬 Type Breakdown — Last 24 h",
            value="\n".join(lines),
            inline=False,
        )
        await interaction.followup.send(embed=e)

    @discord.ui.button(label="Region Stats", emoji="🗺️", style=discord.ButtonStyle.primary)
    async def region_stats_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        region_totals = pokedata.aggregate_regions(self.poke_list)
        if not region_totals:
            await interaction.followup.send("No region data for the last 24 hours.", ephemeral=True)
            return
        lines = [
            f"{E.reply} `{r:<14}` **{c}**"
            for r, c in region_totals.items()
        ]
        e = self._base_embed()
        e.add_field(
            name="🗺️ Region Breakdown — Last 24 h",
            value="\n".join(lines),
            inline=False,
        )
        await interaction.followup.send(embed=e)

    @discord.ui.button(label="Pokémon Caught", emoji="📋", style=discord.ButtonStyle.secondary)
    async def pokemon_list_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        await self._send_poke_page(interaction, 0)

    async def _send_poke_page(self, interaction: discord.Interaction, page: int):
        total_pages = max(1, (len(self.poke_list) + PAGE_SIZE - 1) // PAGE_SIZE)
        chunk = self.poke_list[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        lines = [
            f"{E.reply} `{i + page * PAGE_SIZE + 1:>3}.` **{entry['pokemon']}** × {entry['count']}"
            for i, entry in enumerate(chunk)
        ]
        e = self._base_embed()
        e.add_field(
            name=f"📋 Pokémon Caught — page {page + 1}/{total_pages} (last 24 h)",
            value="\n".join(lines) if lines else "*None yet.*",
            inline=False,
        )
        nav = PokemonListNav(parent=self, page=page, total_pages=total_pages)
        await interaction.followup.send(embed=e, view=nav)


class PokemonListNav(discord.ui.View):
    def __init__(self, parent: ProfileView, page: int, total_pages: int):
        super().__init__(timeout=300)
        self.parent      = parent
        self.page        = page
        self.total_pages = total_pages
        self.prev_btn.disabled = page == 0
        self.next_btn.disabled = page >= total_pages - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        await self.parent._send_poke_page(interaction, self.page - 1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        await self.parent._send_poke_page(interaction, self.page + 1)


# ── Main Cog ──────────────────────────────────────────────────────────────────

class TrackerCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Listener ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != POKETWO_BOT_ID:
            return
        if not message.guild:
            return
        await self._process_poketwo_message(message)

    async def _process_poketwo_message(self, message: discord.Message):
        guild_id   = message.guild.id
        channel_id = message.channel.id
        full_text  = message.content or ""

        catch = pk_parser.parse_catch(full_text)
        if catch:
            await db.record_catch(
                guild_id=guild_id,
                user_id=catch.user_id,
                pokemon=catch.pokemon,
                iv=catch.iv,
                shiny=catch.shiny,
                gigantamax=catch.gigantamax,
                chain_shiny=catch.chain_shiny,
                channel_id=channel_id,
            )
            return ("catch", catch)

        for embed in message.embeds:
            flee = pk_parser.parse_flee(embed.title or "")
            if flee:
                await db.record_flee(guild_id, flee.pokemon, channel_id)
                image_url = pokedata.cdn_image_url(flee.pokemon)
                await self._dispatch_fled_logs(message, guild_id, flee.pokemon, image_url)
                return ("flee", flee)

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
        """Show catch stats: last 24 hours alongside all-time totals."""
        target   = member or ctx.author
        guild_id = ctx.guild.id

        stats, stats_alltime, poke_list, reset_info = (
            await db.get_user_stats(guild_id, target.id),
            await db.get_user_stats_alltime(guild_id, target.id),
            await db.get_user_pokemon_list(guild_id, target.id),
            await db.get_window_reset_info(guild_id),
        )

        if stats_alltime["total"] == 0:
            await ctx.reply(f"No catches recorded for **{target.display_name}** yet.")
            return

        ru   = _reset_unix(reset_info["resets_in_h"])
        view = ProfileView(guild_id, target, stats, stats_alltime, poke_list, ru)
        await ctx.reply(embed=view._base_embed(), view=view)

    # ── a!check ───────────────────────────────────────────────────────────────

    @commands.command(name="check")
    @commands.has_permissions(manage_guild=True)
    async def check(self, ctx: commands.Context):
        """Reply to a Pokétwo message to manually record it."""
        if ctx.message.reference is None:
            await ctx.reply("❌ Please **reply** to the Pokétwo message you want to record.")
            return

        try:
            ref    = ctx.message.reference
            target = ref.resolved or await ctx.channel.fetch_message(ref.message_id)
        except discord.NotFound:
            await ctx.reply("❌ Could not fetch that message.")
            return

        if target.author.id != POKETWO_BOT_ID:
            await ctx.reply(
                f"❌ That message is not from Pokétwo (ID `{POKETWO_BOT_ID}`). "
                "Only Pokétwo messages can be recorded."
            )
            return

        if not target.guild:
            await ctx.reply("❌ That message is not in a server.")
            return

        result = await self._process_poketwo_message(target)

        if result is None:
            await ctx.reply(
                "❌ Could not parse that message as a catch or flee.\n"
                "-# Make sure it is a Pokétwo catch congratulations or fled embed."
            )
            return

        event_type, event = result

        if event_type == "catch":
            flags = []
            if event.shiny:       flags.append(f"{E.shiny} Shiny")
            if event.gigantamax:  flags.append(f"{E.gigantamax} Gigantamax")
            if event.chain_shiny: flags.append(f"{E.chain_shiny} Chain Shiny")
            iv_str   = f"{event.iv:.2f}%" if event.iv is not None else "Hidden"
            flag_str = "  ".join(flags)

            e = discord.Embed(
                title="✅ Catch recorded manually",
                description=(
                    f"{E.reply} **Pokémon:** {event.pokemon}\n"
                    f"{E.reply} **User:** <@{event.user_id}>\n"
                    f"{E.reply} **IV:** {iv_str}"
                    + (f"\n{E.reply} {flag_str}" if flags else "")
                ),
                color=discord.Color.green(),
            )
            e.set_footer(text=f"Added by {ctx.author}")
            await ctx.reply(embed=e)

        else:
            e = discord.Embed(
                title="✅ Flee recorded manually",
                description=f"{E.reply} **Pokémon:** {event.pokemon}",
                color=discord.Color.orange(),
            )
            e.set_footer(text=f"Added by {ctx.author}")
            await ctx.reply(embed=e)

    # ── a!fled-logs ───────────────────────────────────────────────────────────

    @commands.command(name="fled-logs")
    @commands.has_permissions(manage_guild=True)
    async def fled_logs(
        self, ctx: commands.Context, category: str = None, channel_id: str = None
    ):
        """
        Configure where fled-log alerts are sent.

        Usage:
          a!fled-logs <category> <channel_id>
          a!fled-logs list
        """
        if not category:
            await ctx.reply(
                "Usage: `a!fled-logs <category> <channel_id>`  or  `a!fled-logs list`\n"
                f"Available categories: `{'`, `'.join(cats.all_keys())}`"
            )
            return

        if category.lower() == "list":
            configs = await db.get_fled_log_channels(ctx.guild.id)
            if not configs:
                await ctx.reply("No fled-log channels configured yet.")
                return
            lines = [
                f"{E.reply} **{cfg['category_key']}** → "
                + (self.bot.get_channel(cfg["channel_id"]).mention
                   if self.bot.get_channel(cfg["channel_id"])
                   else f"`{cfg['channel_id']}`")
                for cfg in configs
            ]
            e = discord.Embed(
                title="Fled-log routing",
                description="\n".join(lines),
                color=discord.Color.blurple(),
            )
            await ctx.reply(embed=e)
            return

        cat = get_category(category)
        if not cat:
            await ctx.reply(
                f"❌ Unknown category `{category}`.\n"
                f"Available: `{'`, `'.join(cats.all_keys())}`"
            )
            return

        if not channel_id:
            await ctx.reply("❌ Please provide a channel ID or mention.")
            return

        raw_id = re.sub(r"[<#>]", "", channel_id.strip())
        if not raw_id.isdigit():
            await ctx.reply("❌ Invalid channel ID or mention.")
            return

        ch_id = int(raw_id)
        await db.set_fled_log_channel(ctx.guild.id, cat["key"], ch_id)
        ch = self.bot.get_channel(ch_id)
        await ctx.reply(
            f"✅ **{cat['name']}** fled alerts → {ch.mention if ch else f'`{ch_id}`'}"
        )

    # ── a!cleardata ───────────────────────────────────────────────────────────

    @commands.command(name="cleardata")
    async def cleardata(self, ctx: commands.Context):
        """[Owner only] Permanently delete ALL data for this guild."""
        owner_id = OWNER_ID or (await self.bot.application_info()).owner.id
        if ctx.author.id != owner_id:
            await ctx.reply("❌ Only the bot owner can use this command.")
            return

        confirm_embed = discord.Embed(
            title="⚠️ Confirm Data Deletion",
            description=(
                "This will permanently delete **all catches and flees** for this server.\n"
                "This cannot be undone.\n\n"
                "React with ✅ to confirm or ❌ to cancel."
            ),
            color=discord.Color.orange(),
        )
        confirm_msg = await ctx.reply(embed=confirm_embed)
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")

        def check(reaction, user):
            return (
                user == ctx.author
                and reaction.message.id == confirm_msg.id
                and str(reaction.emoji) in ("✅", "❌")
            )

        import asyncio
        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=30.0, check=check)
        except asyncio.TimeoutError:
            await confirm_msg.edit(embed=discord.Embed(
                title="⏱️ Timed out",
                description="Data deletion cancelled.",
                color=discord.Color.greyple(),
            ))
            return

        if str(reaction.emoji) == "❌":
            await confirm_msg.edit(embed=discord.Embed(
                title="❌ Cancelled",
                description="No data was deleted.",
                color=discord.Color.greyple(),
            ))
            return

        deleted = await db.clear_guild_data(ctx.guild.id)
        e = discord.Embed(
            title="🗑️ Data Cleared",
            description=(
                f"{E.reply} **{deleted['catches']}** catch record(s) deleted\n"
                f"{E.reply} **{deleted['flees']}** flee record(s) deleted"
            ),
            color=discord.Color.green(),
        )
        e.set_footer(text=f"Cleared by {ctx.author}")
        await confirm_msg.edit(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(TrackerCog(bot))
