"""
cogs/admin_cog.py — Bot owner admin commands (hybrid: prefix + slash).

After loading, sync slash commands once:
    await bot.tree.sync()

Commands
────────
role add <role_name> [guild_id]        Track a role
role remove <role_name> [guild_id]     Stop tracking a role
role list [guild_id]                   List all tracked roles

admin info server [guild_id]           Server overview embed
admin info user <user> [guild_id]      User overview embed
admin channels [guild_id]              Channel breakdown embed
admin ping                             Show bot latency
admin reload <cog>                     Reload a cog (owner only)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from config import E

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Misc helpers
# ──────────────────────────────────────────────────────────────────────────────

def _utc(dt: datetime) -> datetime:
    """Ensure *dt* is timezone-aware (UTC)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_ts(dt: datetime) -> str:
    """``<t:UNIX:D>  (<t:UNIX:R>)`` Discord timestamp string."""
    unix = int(_utc(dt).timestamp())
    return f"<t:{unix}:D>  (<t:{unix}:R>)"


def _humanise_delta(dt: datetime) -> str:
    """'3 days, 2h 15m ago' style string from a past datetime."""
    diff = datetime.now(timezone.utc) - _utc(dt)
    d, rem = diff.days, diff.seconds
    h, m = rem // 3600, (rem % 3600) // 60

    if d:
        return f"{d} day{'s' if d != 1 else ''}, {h}h {m}m ago"
    if h:
        return f"{h}h {m}m ago"
    return f"{m}m ago"


def _status_icon(status: discord.Status) -> str:
    return {
        discord.Status.online: "🟢",
        discord.Status.idle: "🟡",
        discord.Status.dnd: "🔴",
        discord.Status.offline: "⚫",
    }.get(status, "⚫")


def _verification_label(level: discord.VerificationLevel) -> str:
    return {
        discord.VerificationLevel.none: "None",
        discord.VerificationLevel.low: "Low — verified e-mail",
        discord.VerificationLevel.medium: "Medium — registered 5+ min",
        discord.VerificationLevel.high: "High — member 10+ min",
        discord.VerificationLevel.very_high: "Highest — verified phone",
    }.get(level, "Unknown")


# ──────────────────────────────────────────────────────────────────────────────
# Embed builders
# ──────────────────────────────────────────────────────────────────────────────

def _build_server_embed(guild: discord.Guild) -> discord.Embed:
    text_ch = sum(1 for c in guild.channels if isinstance(c, discord.TextChannel))
    voice_ch = sum(1 for c in guild.channels if isinstance(c, discord.VoiceChannel))
    stage_ch = sum(1 for c in guild.channels if isinstance(c, discord.StageChannel))
    forum_ch = sum(1 for c in guild.channels if isinstance(c, discord.ForumChannel))
    categories = sum(1 for c in guild.channels if isinstance(c, discord.CategoryChannel))

    bots = sum(1 for m in guild.members if m.bot) if guild.members else "N/A"
    humans = (guild.member_count - bots) if isinstance(bots, int) else "N/A"

    embed = discord.Embed(
        title=f"🏢  {guild.name}",
        description=(
            f"**ID:** `{guild.id}`\n"
            f"**Owner:** <@{guild.owner_id}>\n"
            f"**Created:** {_fmt_ts(guild.created_at)}\n"
            f"**Age:** {_humanise_delta(guild.created_at)}"
        ),
        color=discord.Color.blurple(),
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if guild.banner:
        embed.set_image(url=guild.banner.with_format("png").url)

    embed.add_field(
        name="👥 Members",
        value=f"Total: **{guild.member_count:,}**\nHumans: **{humans}**\nBots: **{bots}**",
        inline=True,
    )

    embed.add_field(
        name="📢 Channels",
        value=(
            f"Text: **{text_ch}**\n"
            f"Voice: **{voice_ch}**\n"
            f"Stage: **{stage_ch}** | Forum: **{forum_ch}**\n"
            f"Categories: **{categories}**"
        ),
        inline=True,
    )

    embed.add_field(
        name="🏷️ Roles",
        value=f"**{len(guild.roles)}** total",
        inline=True,
    )

    embed.add_field(
        name="🔒 Verification",
        value=_verification_label(guild.verification_level),
        inline=True,
    )

    embed.add_field(
        name="💬 Locale / Premium",
        value=(
            f"Locale: **{guild.preferred_locale}**\n"
            f"Boost tier: **{guild.premium_tier}** "
            f"({guild.premium_subscription_count} boosts)"
        ),
        inline=True,
    )

    if guild.features:
        shown = sorted(guild.features)[:6]
        extra = len(guild.features) - len(shown)
        feat_text = "  ".join(f"`{f}`" for f in shown)
        if extra:
            feat_text += f"  +{extra} more"
        embed.add_field(name="✨ Features", value=feat_text, inline=False)

    embed.set_footer(text=f"Guild ID: {guild.id}  •  Requested via admin cog")
    return embed


def _build_user_embed(
    user: discord.User | discord.Member,
    member: discord.Member | None,
) -> discord.Embed:
    discriminator = f"#{user.discriminator}" if getattr(user, "discriminator", "0") != "0" else ""

    embed = discord.Embed(
        title=f"👤  {user.display_name}",
        description=(
            f"**Tag:** `{user.name}{discriminator}`\n"
            f"**ID:** `{user.id}`\n"
            f"**Bot:** {'✅ Yes' if user.bot else '❌ No'}"
        ),
        color=discord.Color.purple(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    embed.add_field(
        name="📅 Account Created",
        value=f"{_fmt_ts(user.created_at)}\n{_humanise_delta(user.created_at)}",
        inline=True,
    )

    if member:
        status_icon = _status_icon(member.status) if hasattr(member, "status") else "⚫"
        embed.add_field(
            name="📥 Joined Server",
            value=f"{_fmt_ts(member.joined_at)}\n{_humanise_delta(member.joined_at)}",
            inline=True,
        )

        embed.add_field(
            name="🔵 Status",
            value=f"{status_icon} {member.status}",
            inline=True,
        )

        # Roles (excluding @everyone)
        roles = [r for r in member.roles if r != member.guild.default_role]
        roles.sort(key=lambda r: r.position, reverse=True)
        if roles:
            shown = roles[:12]
            role_text = " ".join(r.mention for r in shown)
            if len(roles) > 12:
                role_text += f"  +{len(roles) - 12} more"
            embed.add_field(name=f"🏷️ Roles ({len(roles)})", value=role_text, inline=False)

        # Key permissions
        key_perms = [
            name.replace("_", " ").title()
            for name, value in member.guild_permissions
            if value and name in {
                "administrator", "manage_guild", "manage_roles",
                "manage_channels", "manage_messages", "kick_members",
                "ban_members", "mention_everyone", "manage_webhooks",
                "view_audit_log",
            }
        ]
        if key_perms:
            embed.add_field(
                name="🔑 Key Permissions",
                value="  ".join(f"`{p}`" for p in key_perms),
                inline=False,
            )

        if member.premium_since:
            embed.add_field(
                name="💎 Boosting Since",
                value=_fmt_ts(member.premium_since),
                inline=True,
            )

    else:
        embed.add_field(
            name="⚠️ Guild Membership",
            value="User is **not** in this guild.",
            inline=False,
        )

    embed.set_footer(text=f"User ID: {user.id}")
    return embed


def _build_channels_embed(guild: discord.Guild) -> discord.Embed:
    categories: list[discord.CategoryChannel] = sorted(
        (c for c in guild.channels if isinstance(c, discord.CategoryChannel)),
        key=lambda c: c.position,
    )
    uncategorised = [
        c for c in guild.channels
        if c.category is None and not isinstance(c, discord.CategoryChannel)
    ]

    embed = discord.Embed(
        title=f"📢  Channel Breakdown — {guild.name}",
        color=discord.Color.green(),
    )

    def _ch_line(ch: discord.abc.GuildChannel) -> str:
        icons = {
            discord.TextChannel: "💬",
            discord.VoiceChannel: "🔊",
            discord.StageChannel: "🎙️",
            discord.ForumChannel: "🗂️",
        }
        icon = icons.get(type(ch), "•")
        nsfw = " 🔞" if getattr(ch, "nsfw", False) else ""
        return f"{icon} {ch.name}{nsfw}"

    # Uncategorised first
    if uncategorised:
        lines = "\n".join(_ch_line(c) for c in uncategorised[:15])
        embed.add_field(name="⬜ Uncategorised", value=lines or "—", inline=False)

    for cat in categories[:20]:
        children = sorted(cat.channels, key=lambda c: c.position)
        lines = "\n".join(_ch_line(c) for c in children[:15])
        if len(children) > 15:
            lines += f"\n… +{len(children) - 15} more"
        embed.add_field(
            name=f"📂 {cat.name.upper()}  ({len(children)})",
            value=lines or "*(empty)*",
            inline=False,
        )

    total = len(guild.channels)
    embed.set_footer(
        text=f"Total channels: {total}  |  Guild ID: {guild.id}"
    )
    return embed


# ──────────────────────────────────────────────────────────────────────────────
# Guild resolver
# ──────────────────────────────────────────────────────────────────────────────

async def _resolve_guild(
    bot: commands.Bot,
    ctx_guild: discord.Guild | None,
    guild_id: int | None,
) -> tuple[discord.Guild | None, str | None]:
    """Return (guild, None) or (None, error_message)."""
    if guild_id is not None:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return None, f"❌ Guild `{guild_id}` not found or bot is not a member."
        return guild, None

    if ctx_guild is None:
        return None, "❌ Run this inside a server or supply a `guild_id`."

    return ctx_guild, None


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog, name="Admin"):
    """Bot-owner administration utilities."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Owner check ──────────────────────────────────────────────────────────

    async def cog_check(self, ctx: commands.Context) -> bool:  # type: ignore[override]
        """All prefix commands in this cog require bot ownership."""
        return await self.bot.is_owner(ctx.author)

    # ──────────────────────────────────────────────────────────────────────────
    # /role  (group)
    # ──────────────────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="role", invoke_without_command=True)
    async def role_group(self, ctx: commands.Context) -> None:
        """Manage tracked roles. Use a subcommand: add, remove, list."""
        if ctx.invoked_subcommand is None:
            await self._role_list(ctx, None)

    # role add ────────────────────────────────────────────────────────────────

    @role_group.command(name="add")
    @app_commands.describe(
        role_name="Name of the role to start tracking",
        guild_id="Target guild ID (defaults to current server)",
    )
    async def role_add(
        self,
        ctx: commands.Context,
        role_name: str,
        guild_id: Optional[int] = None,
    ) -> None:
        """Start tracking a role."""
        guild, err = await _resolve_guild(self.bot, ctx.guild, guild_id)
        if err:
            await ctx.send(err, ephemeral=True)
            return

        role = discord.utils.find(
            lambda r: r.name.lower() == role_name.lower(),
            guild.roles,
        )
        if role is None:
            await ctx.send(
                f"❌ No role named **{role_name}** found in **{guild.name}**.",
                ephemeral=True,
            )
            return

        await db.add_tracked_role(guild.id, role.id)

        embed = discord.Embed(
            description=f"✅ Now tracking {role.mention} (`{role.id}`) in **{guild.name}**.",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    # role remove ─────────────────────────────────────────────────────────────

    @role_group.command(name="remove")
    @app_commands.describe(
        role_name="Name of the role to stop tracking",
        guild_id="Target guild ID (defaults to current server)",
    )
    async def role_remove(
        self,
        ctx: commands.Context,
        role_name: str,
        guild_id: Optional[int] = None,
    ) -> None:
        """Stop tracking a role."""
        guild, err = await _resolve_guild(self.bot, ctx.guild, guild_id)
        if err:
            await ctx.send(err, ephemeral=True)
            return

        role = discord.utils.find(
            lambda r: r.name.lower() == role_name.lower(),
            guild.roles,
        )
        if role is None:
            await ctx.send(
                f"❌ No role named **{role_name}** found in **{guild.name}**.",
                ephemeral=True,
            )
            return

        await db.remove_tracked_role(guild.id, role.id)

        embed = discord.Embed(
            description=f"🗑️ Stopped tracking {role.mention} (`{role.id}`) in **{guild.name}**.",
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    # role list ───────────────────────────────────────────────────────────────

    @role_group.command(name="list")
    @app_commands.describe(guild_id="Target guild ID (defaults to current server)")
    async def role_list(
        self,
        ctx: commands.Context,
        guild_id: Optional[int] = None,
    ) -> None:
        """List all tracked roles."""
        await self._role_list(ctx, guild_id)

    async def _role_list(
        self,
        ctx: commands.Context,
        guild_id: int | None,
    ) -> None:
        guild, err = await _resolve_guild(self.bot, ctx.guild, guild_id)
        if err:
            await ctx.send(err, ephemeral=True)
            return

        tracked_ids: list[int] = await db.get_tracked_roles(guild.id)

        embed = discord.Embed(
            title=f"🏷️  Tracked Roles — {guild.name}",
            color=discord.Color.blurple(),
        )

        if not tracked_ids:
            embed.description = "*No roles are currently being tracked.*"
        else:
            lines: list[str] = []
            for role_id in tracked_ids:
                role = guild.get_role(role_id)
                lines.append(
                    f"• {role.mention}  `{role_id}`"
                    if role
                    else f"• ~~Unknown role~~  `{role_id}`"
                )
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"{len(tracked_ids)} tracked role(s)")

        await ctx.send(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────
    # /admin  (group)
    # ──────────────────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="admin")
    async def admin_group(self, ctx: commands.Context) -> None:
        """Admin utilities group."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    # ── /admin info  (sub-group) ──────────────────────────────────────────────

    @admin_group.group(name="info")
    async def info_group(self, ctx: commands.Context) -> None:
        """Show info about a server or user."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    # admin info server ───────────────────────────────────────────────────────

    @info_group.command(name="server")
    @app_commands.describe(guild_id="Target guild ID (defaults to current server)")
    async def info_server(
        self,
        ctx: commands.Context,
        guild_id: Optional[int] = None,
    ) -> None:
        """Display a detailed server overview."""
        guild, err = await _resolve_guild(self.bot, ctx.guild, guild_id)
        if err:
            await ctx.send(err, ephemeral=True)
            return

        await ctx.send(embed=_build_server_embed(guild))

    # admin info user ─────────────────────────────────────────────────────────

    @info_group.command(name="user")
    @app_commands.describe(
        user="The user to look up",
        guild_id="Target guild ID (defaults to current server)",
    )
    async def info_user(
        self,
        ctx: commands.Context,
        user: discord.User,
        guild_id: Optional[int] = None,
    ) -> None:
        """Display a detailed user overview."""
        guild, err = await _resolve_guild(self.bot, ctx.guild, guild_id)
        if err:
            await ctx.send(err, ephemeral=True)
            return

        member: discord.Member | None = guild.get_member(user.id)
        await ctx.send(embed=_build_user_embed(user, member))

    # ── admin channels ────────────────────────────────────────────────────────

    @admin_group.command(name="channels")
    @app_commands.describe(guild_id="Target guild ID (defaults to current server)")
    async def admin_channels(
        self,
        ctx: commands.Context,
        guild_id: Optional[int] = None,
    ) -> None:
        """Show a full channel breakdown, grouped by category."""
        guild, err = await _resolve_guild(self.bot, ctx.guild, guild_id)
        if err:
            await ctx.send(err, ephemeral=True)
            return

        await ctx.send(embed=_build_channels_embed(guild))

    # ── admin ping ────────────────────────────────────────────────────────────

    @admin_group.command(name="ping")
    async def admin_ping(self, ctx: commands.Context) -> None:
        """Show the bot's WebSocket and REST latency."""
        ws_latency = round(self.bot.latency * 1000)

        # Measure REST round-trip
        before = discord.utils.utcnow()
        msg = await ctx.send("📡 Pinging…")
        rest_latency = round((discord.utils.utcnow() - before).total_seconds() * 1000)

        colour = (
            discord.Color.green() if ws_latency < 100
            else discord.Color.orange() if ws_latency < 250
            else discord.Color.red()
        )

        embed = discord.Embed(title="🏓 Pong!", color=colour)
        embed.add_field(name="WebSocket", value=f"`{ws_latency} ms`", inline=True)
        embed.add_field(name="REST", value=f"`{rest_latency} ms`", inline=True)
        await msg.edit(content=None, embed=embed)

    # ── admin reload ──────────────────────────────────────────────────────────

    @admin_group.command(name="reload")
    @commands.is_owner()
    @app_commands.describe(cog="Dotted path to the cog extension (e.g. cogs.admin_cog)")
    async def admin_reload(self, ctx: commands.Context, cog: str) -> None:
        """Reload a cog extension. (Owner only)"""
        try:
            await self.bot.reload_extension(cog)
        except commands.ExtensionNotLoaded:
            await ctx.send(f"❌ Extension `{cog}` is not loaded.", ephemeral=True)
        except commands.ExtensionNotFound:
            await ctx.send(f"❌ Extension `{cog}` not found.", ephemeral=True)
        except Exception as exc:
            log.exception("Failed to reload %s", cog)
            await ctx.send(f"❌ Reload failed:\n```\n{exc}\n```", ephemeral=True)
        else:
            await ctx.send(f"♻️ Reloaded `{cog}` successfully.", ephemeral=True)


# ──────────────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
