"""
cogs/admin_cog.py — Pure prefix-only admin commands.

Commands:
    !roleadd    @user <role name>   Assign a role to a user
    !roleremove @user <role name>   Remove a role from a user
    !rolelist                       List all server roles sorted by position (highest first)
    !userinfo   <@user or user_id>  Detailed user overview
    !serverinfo                     Detailed server overview
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Custom check — passes if the invoker is the bot owner OR has the given perm
# ──────────────────────────────────────────────────────────────────────────────

def owner_or_permissions(**perms):
    """
    Decorator that allows the command if the caller is the bot owner
    OR has all of the specified guild permissions.
    """
    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        # Fall back to the normal permission check
        missing = [
            perm for perm, value in perms.items()
            if getattr(ctx.permissions, perm, None) != value
        ]
        if missing:
            raise commands.MissingPermissions(missing)
        return True
    return commands.check(predicate)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_ts(dt: datetime) -> str:
    unix = int(_utc(dt).timestamp())
    return f"<t:{unix}:D>  (<t:{unix}:R>)"


def _humanise_delta(dt: datetime) -> str:
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
        discord.Status.online:  "🟢",
        discord.Status.idle:    "🟡",
        discord.Status.dnd:     "🔴",
        discord.Status.offline: "⚫",
    }.get(status, "⚫")


def _verification_label(level: discord.VerificationLevel) -> str:
    return {
        discord.VerificationLevel.none:      "None",
        discord.VerificationLevel.low:       "Low — verified e-mail",
        discord.VerificationLevel.medium:    "Medium — registered 5+ min",
        discord.VerificationLevel.high:      "High — member 10+ min",
        discord.VerificationLevel.very_high: "Highest — verified phone",
    }.get(level, "Unknown")


# ──────────────────────────────────────────────────────────────────────────────
# Embed builders
# ──────────────────────────────────────────────────────────────────────────────

def _build_server_embed(guild: discord.Guild) -> discord.Embed:
    text_ch  = sum(1 for c in guild.channels if isinstance(c, discord.TextChannel))
    voice_ch = sum(1 for c in guild.channels if isinstance(c, discord.VoiceChannel))
    stage_ch = sum(1 for c in guild.channels if isinstance(c, discord.StageChannel))
    forum_ch = sum(1 for c in guild.channels if isinstance(c, discord.ForumChannel))
    cats     = sum(1 for c in guild.channels if isinstance(c, discord.CategoryChannel))

    bots   = sum(1 for m in guild.members if m.bot) if guild.members else "N/A"
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
            f"Text: **{text_ch}**\nVoice: **{voice_ch}**\n"
            f"Stage: **{stage_ch}** | Forum: **{forum_ch}**\nCategories: **{cats}**"
        ),
        inline=True,
    )
    embed.add_field(name="🏷️ Roles",       value=f"**{len(guild.roles)}** total",               inline=True)
    embed.add_field(name="🔒 Verification", value=_verification_label(guild.verification_level), inline=True)
    embed.add_field(
        name="💬 Locale / Premium",
        value=(
            f"Locale: **{guild.preferred_locale}**\n"
            f"Boost tier: **{guild.premium_tier}** ({guild.premium_subscription_count} boosts)"
        ),
        inline=True,
    )
    if guild.features:
        shown     = sorted(guild.features)[:6]
        extra     = len(guild.features) - len(shown)
        feat_text = "  ".join(f"`{f}`" for f in shown)
        if extra:
            feat_text += f"  +{extra} more"
        embed.add_field(name="✨ Features", value=feat_text, inline=False)

    embed.set_footer(text=f"Guild ID: {guild.id}")
    return embed


def _build_user_embed(member: discord.Member) -> discord.Embed:
    user = member._user  # underlying User object
    discriminator = f"#{user.discriminator}" if getattr(user, "discriminator", "0") != "0" else ""

    embed = discord.Embed(
        title=f"👤  {member.display_name}",
        description=(
            f"**Tag:** `{user.name}{discriminator}`\n"
            f"**ID:** `{user.id}`\n"
            f"**Bot:** {'✅ Yes' if user.bot else '❌ No'}"
        ),
        color=discord.Color.purple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="📅 Account Created",
        value=f"{_fmt_ts(user.created_at)}\n{_humanise_delta(user.created_at)}",
        inline=True,
    )
    embed.add_field(
        name="📥 Joined Server",
        value=f"{_fmt_ts(member.joined_at)}\n{_humanise_delta(member.joined_at)}",
        inline=True,
    )

    status_icon = _status_icon(member.status)
    embed.add_field(name="🔵 Status", value=f"{status_icon} {member.status}", inline=True)

    roles = [r for r in member.roles if r != member.guild.default_role]
    roles.sort(key=lambda r: r.position, reverse=True)
    if roles:
        shown     = roles[:12]
        role_text = " ".join(r.mention for r in shown)
        if len(roles) > 12:
            role_text += f"  +{len(roles) - 12} more"
        embed.add_field(name=f"🏷️ Roles ({len(roles)})", value=role_text, inline=False)

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
        embed.add_field(name="💎 Boosting Since", value=_fmt_ts(member.premium_since), inline=True)

    embed.set_footer(text=f"User ID: {user.id}")
    return embed


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog, name="Admin"):
    """Prefix-only admin utilities."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── !roleadd @user <role name> ────────────────────────────────────────────

    @commands.command(name="roleadd")
    @owner_or_permissions(manage_roles=True)
    async def roleadd(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        role_name: str,
    ) -> None:
        """Assign a role to a member.  Usage: !roleadd @user Role Name"""
        role = discord.utils.find(
            lambda r: r.name.lower() == role_name.lower(), ctx.guild.roles
        )
        if role is None:
            await ctx.send(f"❌ No role named **{role_name}** found.")
            return
        if role in member.roles:
            await ctx.send(f"⚠️ {member.mention} already has **{role.name}**.")
            return
        if role >= ctx.guild.me.top_role:
            await ctx.send("❌ That role is equal to or above my highest role — I can't assign it.")
            return

        await member.add_roles(role, reason=f"roleadd by {ctx.author}")
        embed = discord.Embed(
            description=f"✅ Added {role.mention} to {member.mention}.",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @roleadd.error
    async def roleadd_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ Usage: `!roleadd @user <role name>`")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Member not found. Mention them or use their ID.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need the **Manage Roles** permission.")
        else:
            raise error

    # ── !roleremove @user <role name> ─────────────────────────────────────────

    @commands.command(name="roleremove")
    @owner_or_permissions(manage_roles=True)
    async def roleremove(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        role_name: str,
    ) -> None:
        """Remove a role from a member.  Usage: !roleremove @user Role Name"""
        role = discord.utils.find(
            lambda r: r.name.lower() == role_name.lower(), ctx.guild.roles
        )
        if role is None:
            await ctx.send(f"❌ No role named **{role_name}** found.")
            return
        if role not in member.roles:
            await ctx.send(f"⚠️ {member.mention} doesn't have **{role.name}**.")
            return
        if role >= ctx.guild.me.top_role:
            await ctx.send("❌ That role is equal to or above my highest role — I can't remove it.")
            return

        await member.remove_roles(role, reason=f"roleremove by {ctx.author}")
        embed = discord.Embed(
            description=f"🗑️ Removed {role.mention} from {member.mention}.",
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    @roleremove.error
    async def roleremove_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ Usage: `!roleremove @user <role name>`")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Member not found. Mention them or use their ID.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need the **Manage Roles** permission.")
        else:
            raise error

    # ── !rolelist ─────────────────────────────────────────────────────────────

    @commands.command(name="rolelist")
    async def rolelist(self, ctx: commands.Context) -> None:
        """List every server role sorted by position, highest first."""
        # Skip @everyone, sort descending by position
        roles = sorted(
            (r for r in ctx.guild.roles if r.name != "@everyone"),
            key=lambda r: r.position,
            reverse=True,
        )

        if not roles:
            await ctx.send("No roles found (besides @everyone).")
            return

        lines = []
        for i, role in enumerate(roles, start=1):
            member_count = len(role.members)
            lines.append(f"`{i:>2}.` {role.mention}  ─  `{member_count}` member{'s' if member_count != 1 else ''}")

        # Split into pages of 20 if the server has many roles
        page_size = 20
        pages = [lines[i:i + page_size] for i in range(0, len(lines), page_size)]

        for page_num, page in enumerate(pages, start=1):
            embed = discord.Embed(
                title=f"🏷️  Role List — {ctx.guild.name}"
                      + (f"  (page {page_num}/{len(pages)})" if len(pages) > 1 else ""),
                description="\n".join(page),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"{len(roles)} roles total  •  sorted highest → lowest")
            await ctx.send(embed=embed)

    # ── !userinfo <@user or id> ───────────────────────────────────────────────

    @commands.command(name="userinfo")
    async def userinfo(
        self,
        ctx: commands.Context,
        *,
        target: str,
    ) -> None:
        """Show detailed info about a user.  Usage: !userinfo @user  or  !userinfo 123456789"""
        # Try to resolve as a Member (handles mention, name#discrim, plain ID)
        member: discord.Member | None = None

        # Strip <@> mention formatting if present
        raw = target.strip().lstrip("<@").rstrip(">").lstrip("!")

        if raw.isdigit():
            member = ctx.guild.get_member(int(raw))
            if member is None:
                # Try fetching — may not be cached
                try:
                    member = await ctx.guild.fetch_member(int(raw))
                except discord.NotFound:
                    pass

        # Fallback: search by name
        if member is None:
            member = discord.utils.find(
                lambda m: m.name.lower() == target.lower()
                or m.display_name.lower() == target.lower(),
                ctx.guild.members,
            )

        if member is None:
            await ctx.send("❌ Member not found in this server.")
            return

        await ctx.send(embed=_build_user_embed(member))

    @userinfo.error
    async def userinfo_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ Usage: `!userinfo @user`  or  `!userinfo <user_id>`")
        else:
            raise error

    # ── !serverinfo ───────────────────────────────────────────────────────────

    @commands.command(name="serverinfo")
    async def serverinfo(self, ctx: commands.Context) -> None:
        """Display a detailed overview of this server."""
        await ctx.send(embed=_build_server_embed(ctx.guild))


# ──────────────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
