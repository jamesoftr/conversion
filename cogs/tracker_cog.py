"""
cogs/tracker_cog.py  —  Pokétwo catch & flee tracker.

Stats commands show BOTH last-24-hour and all-time totals.
Data is stored permanently — nothing is auto-deleted.

Commands
────────
a!profile [@user]                      — Catch profile (24 h + all-time)
a!check                                — Reply to a Pokétwo msg to manually record it
a!fled-logs <category> <channel_id>   — Admin: route fled alerts to a channel
a!fled-logs list                       — Admin: show current routing
"""

import re
import discord
from discord.ext import commands

import db
import parser as pk_parser
import pokedata
import categories as cats
from categories import get_category, get_category_for_pokemon

# Pokétwo's official bot user ID
POKETWO_BOT_ID = 716390085896962058

# Items per page in the Pokémon list
PAGE_SIZE = 15

# Bot owner ID — only this user may run a!cleardata
OWNER_ID = None  # set via env or replace with your Discord user ID (int)


# ── Profile View ──────────────────────────────────────────────────────────────

class ProfileView(discord.ui.View):
    def __init__(
        self,
        guild_id:       int,
        target:         discord.Member | discord.User,
        stats:          dict,   # last 24 h
        stats_alltime:  dict,   # all-time
        poke_list:      list[dict],          # last 24 h
        poke_list_all:  list[dict],          # all-time
        reset_info:     dict,
    ):
        super().__init__(timeout=300)
        self.guild_id      = guild_id
        self.target        = target
        self.stats         = stats
        self.stats_alltime = stats_alltime
        self.poke_list     = poke_list
        self.poke_list_all = poke_list_all
        self.reset_info    = reset_info

    def _base_embed(self) -> discord.Embed:
        s   = self.stats
        sa  = self.stats_alltime
        ri  = self.reset_info

        shiny_24h  = s["shiny"]  + s["chain_shiny"]
        shiny_all  = sa["shiny"] + sa["chain_shiny"]

        e = discord.Embed(
            title=f"🎮 {self.target.display_name}",
            color=discord.Color.gold(),
        )
        e.set_thumbnail(url=self.target.display_avatar.url)

        # 24-h column
        e.add_field(
            name="📅 Last 24 Hours",
            value=(
                f"Catches: **{s['total']}**\n"
                f"✨ Shiny: **{shiny_24h}**\n"
                f"🔴 Gigantamax: **{s['gigantamax']}**\n"
                f"🔗 Chain Shiny: **{s['chain_shiny']}**"
            ),
            inline=True,
        )
        # all-time column
        e.add_field(
            name="🏅 All Time",
            value=(
                f"Catches: **{sa['total']}**\n"
                f"✨ Shiny: **{shiny_all}**\n"
                f"🔴 Gigantamax: **{sa['gigantamax']}**\n"
                f"🔗 Chain Shiny: **{sa['chain_shiny']}**"
            ),
            inline=True,
        )

        reset_str = db.fmt_reset(ri["resets_in_h"])
        e.set_footer(text=f"24-hour window · {reset_str}")
        return e

    # ── Type Stats ────────────────────────────────────────────────────────────

    @discord.ui.button(label="Type Stats", emoji="🔬", style=discord.ButtonStyle.primary)
    async def type_stats_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        type_totals = pokedata.aggregate_types(self.poke_list)
        if not type_totals:
            await interaction.followup.send("No type data available for the last 24 hours.")
            return
        lines = [f"`{t:<12}` {c}" for t, c in list(type_totals.items())[:25]]
        e = self._base_embed()
        e.add_field(name="— Type Breakdown (last 24 h) —", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=e)

    # ── Region Stats ──────────────────────────────────────────────────────────

    @discord.ui.button(label="Region Stats", emoji="🗺️", style=discord.ButtonStyle.primary)
    async def region_stats_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        region_totals = pokedata.aggregate_regions(self.poke_list)
        if not region_totals:
            await interaction.followup.send("No region data available for the last 24 hours.")
            return
        lines = [f"`{r:<14}` {c}" for r, c in region_totals.items()]
        e = self._base_embed()
        e.add_field(name="— Region Breakdown (last 24 h) —", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=e)

    # ── Pokémon List ──────────────────────────────────────────────────────────

    @discord.ui.button(label="Pokémon Caught", emoji="📋", style=discord.ButtonStyle.secondary)
    async def pokemon_list_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        await self._send_poke_page(interaction, 0)

    async def _send_poke_page(self, interaction: discord.Interaction, page: int):
        total_pages = max(1, (len(self.poke_list) + PAGE_SIZE - 1) // PAGE_SIZE)
        chunk = self.poke_list[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        lines = [
            f"`{i + page * PAGE_SIZE + 1:>3}.` **{entry['pokemon']}** × {entry['count']}"
            for i, entry in enumerate(chunk)
        ]
        e = self._base_embed()
        e.add_field(
            name=f"— Pokémon Caught — page {page + 1}/{total_pages} (last 24 h) —",
            value="\n".join(lines) if lines else "None yet.",
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
        # Only ever process messages from the official Pokétwo bot
        if message.author.id != POKETWO_BOT_ID:
            return
        if not message.guild:
            return
        await self._process_poketwo_message(message)

    async def _process_poketwo_message(self, message: discord.Message):
        """
        Parse a Pokétwo message and record the catch or flee.
        Returns ("catch", CatchEvent) | ("flee", FleeEvent) | None.
        Shared by the live listener and the manual a!check command.
        """
        guild_id   = message.guild.id
        channel_id = message.channel.id
        full_text  = message.content or ""

        # Catch
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

        # Flee (embed title)
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
        guild_id: int,
        pokemon: str,
        image_url: str | None,
    ):
        """Send a fled-log alert to any channels configured for this Pokémon's categories."""
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
                    f"**Category:** {cat['name']}\n"
                    f"**Spotted in:** {original_msg.channel.mention}"
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

        stats, stats_alltime, poke_list, poke_list_all, reset_info = (
            await db.get_user_stats(guild_id, target.id),
            await db.get_user_stats_alltime(guild_id, target.id),
            await db.get_user_pokemon_list(guild_id, target.id),
            await db.get_user_pokemon_list_alltime(guild_id, target.id),
            await db.get_window_reset_info(guild_id),
        )

        if stats_alltime["total"] == 0:
            await ctx.reply(f"No catches recorded for **{target.display_name}** yet.")
            return

        view  = ProfileView(guild_id, target, stats, stats_alltime, poke_list, poke_list_all, reset_info)
        embed = view._base_embed()
        await ctx.reply(embed=embed, view=view)

    # ── a!check (manual backfill) ─────────────────────────────────────────────

    @commands.command(name="check")
    @commands.has_permissions(manage_guild=True)
    async def check(self, ctx: commands.Context):
        """
        Reply to a Pokétwo message to manually add its catch or flee to the records.

        Usage: reply to any Pokétwo catch/flee message, then type `a!check`
        """
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
            if event.shiny:       flags.append("✨ Shiny")
            if event.gigantamax:  flags.append("🔴 Gigantamax")
            if event.chain_shiny: flags.append("🔗 Chain Shiny")
            iv_str   = f"{event.iv:.2f}%" if event.iv is not None else "Hidden"
            flag_str = "  " + "  ".join(flags) if flags else ""

            e = discord.Embed(
                title="✅ Catch recorded manually",
                description=(
                    f"**Pokémon:** {event.pokemon}\n"
                    f"**User:** <@{event.user_id}>\n"
                    f"**IV:** {iv_str}"
                    + (f"\n{flag_str}" if flags else "")
                ),
                color=discord.Color.green(),
            )
            e.set_footer(text=f"Added by {ctx.author}")
            await ctx.reply(embed=e)

        else:
            e = discord.Embed(
                title="✅ Flee recorded manually",
                description=f"**Pokémon:** {event.pokemon}",
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
          a!fled-logs <category> <channel_id>   — Set routing
          a!fled-logs list                       — Show current config
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
            lines = []
            for cfg in configs:
                ch     = self.bot.get_channel(cfg["channel_id"])
                ch_str = ch.mention if ch else f"`{cfg['channel_id']}`"
                lines.append(f"**{cfg['category_key']}** → {ch_str}")
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
        """
        [Owner only] Permanently delete ALL catch and flee data for this guild.

        Usage: a!cleardata
        """
        owner_id = OWNER_ID or (await self.bot.application_info()).owner.id
        if ctx.author.id != owner_id:
            await ctx.reply("❌ Only the bot owner can use this command.")
            return

        confirm_embed = discord.Embed(
            title="⚠️ Confirm Data Deletion",
            description=(
                "This will permanently delete **all catches and flees** recorded "
                "for this server (all time — cannot be undone).\n\n"
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
                title="⏱️ Timed out", description="Data deletion cancelled.", color=discord.Color.greyple()
            ))
            return

        if str(reaction.emoji) == "❌":
            await confirm_msg.edit(embed=discord.Embed(
                title="❌ Cancelled", description="No data was deleted.", color=discord.Color.greyple()
            ))
            return

        deleted = await db.clear_guild_data(ctx.guild.id)
        e = discord.Embed(
            title="🗑️ Data Cleared",
            description=(
                f"Deleted **{deleted['catches']}** catch record(s) and "
                f"**{deleted['flees']}** flee record(s) for this server."
            ),
            color=discord.Color.green(),
        )
        e.set_footer(text=f"Cleared by {ctx.author}")
        await confirm_msg.edit(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(TrackerCog(bot))
