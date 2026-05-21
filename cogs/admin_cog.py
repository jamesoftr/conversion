"""
cogs/admin_cog.py  —  Bot owner admin commands.

Features
────────
  • Role management (add, remove, list tracked roles)
  • Server info (member count, channel count, creation date, etc.)
  • User info (join date, roles, account age, etc.)
  • Database operations (tracked roles per guild)

Commands  (prefix /)
────────────────────
  /role add <role_name> [guild_id]           — Add a tracked role
  /role remove <role_name> [guild_id]        — Remove a tracked role
  /role list [guild_id]                      — List all tracked roles
  /admin info server [guild_id]              — Server information
  /admin info user <user_id>                 — User information
  /admin channels <guild_id>                 — Count channels in server
"""

from __future__ import annotations

import discord
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional

import db
from config import E   # emoji config; adjust if needed


# ── Constants ─────────────────────────────────────────────────────────────────

ROLE_STORAGE_KEY = "tracked_roles"  # Used for database if you want to persist


# ── Formatting helpers ────────────────────────────────────────────────────────

def _format_datetime(dt: datetime) -> str:
    """Format datetime as Discord timestamp."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix = int(dt.timestamp())
    return f"<t:{unix}:D> (<t:{unix}:R>)"


def _duration_since(dt: datetime) -> str:
    """Calculate human-readable duration since a datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    
    now = datetime.now(timezone.utc)
    diff = now - dt
    
    days = diff.days
    hours = (diff.seconds // 3600) % 24
    minutes = (diff.seconds // 60) % 60
    
    if days > 0:
        return f"{days} day{'s' if days != 1 else ''}, {hours}h {minutes}m ago"
    elif hours > 0:
        return f"{hours}h {minutes}m ago"
    else:
        return f"{minutes}m ago"


# ── Embed builders ────────────────────────────────────────────────────────────

def _server_info_embed(guild: discord.Guild) -> discord.Embed:
    """Full server information embed."""
    embed = discord.Embed(
        title=f"🏢 Server Info — {guild.name}",
        color=discord.Color.blue(),
    )
    
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    
    embed.add_field(name="Server ID", value=f"`{guild.id}`", inline=True)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Created", value=_format_datetime(guild.created_at), inline=True)
    
    embed.add_field(name="Members", value=f"👥 {guild.member_count:,}", inline=True)
    embed.add_field(name="Roles", value=f"🏷️ {len(guild.roles)}", inline=True)
    embed.add_field(name="Channels", value=f"📢 {len(guild.channels)}", inline=True)
    
    # Break down channels by type
    text_channels = len([c for c in guild.channels if isinstance(c, discord.TextChannel)])
    voice_channels = len([c for c in guild.channels if isinstance(c, discord.VoiceChannel)])
    categories = len([c for c in guild.channels if isinstance(c, discord.CategoryChannel)])
    
    embed.add_field(
        name="Channel Breakdown",
        value=f"📝 Text: {text_channels} | 🔊 Voice: {voice_channels} | 📂 Categories: {categories}",
        inline=False,
    )
    
    # Features
    if guild.features:
        features = ", ".join([f"`{f}`" for f in sorted(guild.features)[:5]])
        if len(guild.features) > 5:
            features += f" +{len(guild.features) - 5} more"
        embed.add_field(name="Features", value=features, inline=False)
    
    # Verification level
    levels = {
        discord.VerificationLevel.none: "None",
        discord.VerificationLevel.low: "Low",
        discord.VerificationLevel.medium: "Medium",
        discord.VerificationLevel.high: "High",
        discord.VerificationLevel.very_high: "Very High",
    }
    embed.add_field(
        name="Verification Level",
        value=levels.get(guild.verification_level, "Unknown"),
        inline=True,
    )
    
    embed.set_footer(text=f"Guild ID: {guild.id}")
    return embed


def _user_info_embed(user: discord.User, guild: discord.Guild = None) -> discord.Embed:
    """Full user information embed."""
    embed = discord.Embed(
        title=f"👤 User Info — {user.display_name}",
        color=discord.Color.purple(),
    )
    
    embed.set_thumbnail(url=user.display_avatar.url)
    
    embed.add_field(name="Username", value=f"`{user.name}#{user.discriminator if user.discriminator != '0' else ''}`", inline=True)
    embed.add_field(name="User ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="Bot", value="✅ Yes" if user.bot else "❌ No", inline=True)
    
    embed.add_field(name="Account Created", value=_format_datetime(user.created_at), inline=True)
    embed.add_field(name="Account Age", value=_duration_since(user.created_at), inline=True)
    
    # If member context is available
    if guild:
        try:
            member = await guild.fetch_member(user.id)
            embed.add_field(name="Joined Server", value=_format_datetime(member.joined_at), inline=True)
            embed.add_field(name="Member Since", value=_duration_since(member.joined_at), inline=True)
            
            # Roles (excluding @everyone)
            roles = [r.mention for r in member.roles if r != guild.default_role]
            if roles:
                roles_str = ", ".join(roles[:10])
                if len(roles) > 10:
                    roles_str += f" +{len(roles) - 10} more"
                embed.add_field(name="Roles", value=roles_str, inline=False)
            
            # Permissions (top 5)
            perms = [p for p, v in member.guild_permissions if v]
            if perms:
                perms_str = ", ".join([f"`{p}`" for p in perms[:5]])
                if len(perms) > 5:
                    perms_str += f" +{len(perms) - 5} more"
                embed.add_field(name="Key Permissions", value=perms_str, inline=False)
        except discord.NotFound:
            embed.add_field(name="Guild Member", value="❌ Not in this guild", inline=False)
    
    embed.set_footer(text=f"User ID: {user.id}")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog):
    """Bot owner admin commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory storage for tracked roles per guild
        # Format: {guild_id: {"role_name": role_id, ...}}
        self.tracked_roles = {}

    def _check_owner(self, user: discord.User) -> bool:
        """Check if user is bot owner."""
        return user.id == self.bot.owner_id

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Global check: only bot owner can use these commands."""
        if not self._check_owner(ctx.author):
            raise commands.NotOwner()
        return True

    # ── Group: /role ─────────────────────────────────────────────────────────

    @commands.group(name="role", invoke_without_command=True)
    async def role(self, ctx: commands.Context):
        """Manage tracked roles. Use `/role help` for subcommands."""
        await ctx.reply(
            "**Role Management Commands:**\n"
            "`/role add <role_name> [guild_id]` — Add a tracked role\n"
            "`/role remove <role_name> [guild_id]` — Remove a tracked role\n"
            "`/role list [guild_id]` — List tracked roles\n\n"
            "If `guild_id` is omitted, the current guild is used.",
            mention_author=False,
        )

    # ── /role add ────────────────────────────────────────────────────────────

    @role.command(name="add")
    async def role_add(self, ctx: commands.Context, role_name: str, guild_id: int = None):
        """
        Add a role to the tracking list.

        Examples:
          /role add Admin
          /role add Moderator 123456789
        """
        if guild_id is None:
            guild_id = ctx.guild.id
            guild = ctx.guild
        else:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                await ctx.reply(
                    f"❌ Guild with ID `{guild_id}` not found or bot is not in that guild.",
                    mention_author=False,
                )
                return

        # Find the role by name
        role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
        if not role:
            await ctx.reply(
                f"❌ Role `{role_name}` not found in **{guild.name}**.",
                mention_author=False,
            )
            return

        # Initialize tracking dict for this guild if needed
        if guild_id not in self.tracked_roles:
            self.tracked_roles[guild_id] = {}

        # Add the role
        self.tracked_roles[guild_id][role_name.lower()] = role.id

        embed = discord.Embed(
            title="✅ Role Added",
            description=f"Role `{role.name}` (`{role.id}`) added to tracking in **{guild.name}**.",
            color=discord.Color.green(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── /role remove ─────────────────────────────────────────────────────────

    @role.command(name="remove")
    async def role_remove(self, ctx: commands.Context, role_name: str, guild_id: int = None):
        """
        Remove a role from the tracking list.

        Examples:
          /role remove Admin
          /role remove Moderator 123456789
        """
        if guild_id is None:
            guild_id = ctx.guild.id
            guild = ctx.guild
        else:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                await ctx.reply(
                    f"❌ Guild with ID `{guild_id}` not found or bot is not in that guild.",
                    mention_author=False,
                )
                return

        if guild_id not in self.tracked_roles or role_name.lower() not in self.tracked_roles[guild_id]:
            await ctx.reply(
                f"❌ Role `{role_name}` is not being tracked in **{guild.name}**.",
                mention_author=False,
            )
            return

        del self.tracked_roles[guild_id][role_name.lower()]

        embed = discord.Embed(
            title="❌ Role Removed",
            description=f"Role `{role_name}` removed from tracking in **{guild.name}**.",
            color=discord.Color.red(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ── /role list ───────────────────────────────────────────────────────────

    @role.command(name="list")
    async def role_list(self, ctx: commands.Context, guild_id: int = None):
        """
        List all tracked roles in a guild.

        Examples:
          /role list
          /role list 123456789
        """
        if guild_id is None:
            guild_id = ctx.guild.id
            guild = ctx.guild
        else:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                await ctx.reply(
                    f"❌ Guild with ID `{guild_id}` not found or bot is not in that guild.",
                    mention_author=False,
                )
                return

        if guild_id not in self.tracked_roles or not self.tracked_roles[guild_id]:
            await ctx.reply(
                f"📭 No tracked roles in **{guild.name}**.",
                mention_author=False,
            )
            return

        tracked = self.tracked_roles[guild_id]
        lines = []
        for role_name, role_id in sorted(tracked.items()):
            role = guild.get_role(role_id)
            if role:
                lines.append(f"  • `{role.name}` — ID: `{role_id}` — Members: {len(role.members)}")
            else:
                lines.append(f"  • `{role_name}` — ID: `{role_id}` — ⚠️ (role deleted)")

        embed = discord.Embed(
            title=f"🏷️ Tracked Roles — {guild.name}",
            description="\n".join(lines) if lines else "*No tracked roles.*",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Guild ID: {guild_id} | Total: {len(tracked)}")
        await ctx.reply(embed=embed, mention_author=False)

    # ── Group: /admin ────────────────────────────────────────────────────────

    @commands.group(name="admin", invoke_without_command=True)
    async def admin(self, ctx: commands.Context):
        """Admin utility commands. Use `/admin help` for subcommands."""
        await ctx.reply(
            "**Admin Commands:**\n"
            "`/admin info server [guild_id]` — Server information\n"
            "`/admin info user <user_id>` — User information\n"
            "`/admin channels <guild_id>` — Channel count in server",
            mention_author=False,
        )

    # ── /admin info ──────────────────────────────────────────────────────────

    @admin.group(name="info", invoke_without_command=True)
    async def admin_info(self, ctx: commands.Context):
        """Info subcommands."""
        await ctx.reply(
            "**Info Subcommands:**\n"
            "`/admin info server [guild_id]` — Server information\n"
            "`/admin info user <user_id>` — User information",
            mention_author=False,
        )

    @admin_info.command(name="server")
    async def admin_info_server(self, ctx: commands.Context, guild_id: int = None):
        """
        Show detailed information about a server.

        Examples:
          /admin info server           (current guild)
          /admin info server 123456789 (specific guild)
        """
        if guild_id is None:
            guild = ctx.guild
        else:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                await ctx.reply(
                    f"❌ Guild with ID `{guild_id}` not found or bot is not in that guild.",
                    mention_author=False,
                )
                return

        embed = _server_info_embed(guild)
        await ctx.reply(embed=embed, mention_author=False)

    @admin_info.command(name="user")
    async def admin_info_user(self, ctx: commands.Context, user_id: int):
        """
        Show detailed information about a user.

        Examples:
          /admin info user 123456789
          /admin info user @User
        """
        try:
            user = await self.bot.fetch_user(user_id)
        except discord.NotFound:
            await ctx.reply(
                f"❌ User with ID `{user_id}` not found.",
                mention_author=False,
            )
            return

        # Try to get member info from current guild
        member = None
        if ctx.guild:
            try:
                member = await ctx.guild.fetch_member(user_id)
            except discord.NotFound:
                pass

        embed = _user_info_embed(user, ctx.guild)
        await ctx.reply(embed=embed, mention_author=False)

    # ── /admin channels ──────────────────────────────────────────────────────

    @admin.command(name="channels")
    async def admin_channels(self, ctx: commands.Context, guild_id: int = None):
        """
        Get a detailed channel count for a server.

        Examples:
          /admin channels           (current guild)
          /admin channels 123456789 (specific guild)
        """
        if guild_id is None:
            guild = ctx.guild
        else:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                await ctx.reply(
                    f"❌ Guild with ID `{guild_id}` not found or bot is not in that guild.",
                    mention_author=False,
                )
                return

        # Count by type
        text_channels = [c for c in guild.channels if isinstance(c, discord.TextChannel)]
        voice_channels = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
        stage_channels = [c for c in guild.channels if isinstance(c, discord.StageChannel)]
        categories = [c for c in guild.channels if isinstance(c, discord.CategoryChannel)]
        forum_channels = [c for c in guild.channels if isinstance(c, discord.ForumChannel)]

        embed = discord.Embed(
            title=f"📢 Channels — {guild.name}",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="📝 Text Channels",
            value=f"{len(text_channels)} channels",
            inline=True,
        )
        embed.add_field(
            name="🔊 Voice Channels",
            value=f"{len(voice_channels)} channels",
            inline=True,
        )
        embed.add_field(
            name="🎙️ Stage Channels",
            value=f"{len(stage_channels)} channels",
            inline=True,
        )
        embed.add_field(
            name="📂 Categories",
            value=f"{len(categories)} categories",
            inline=True,
        )
        embed.add_field(
            name="💬 Forum Channels",
            value=f"{len(forum_channels)} channels",
            inline=True,
        )
        embed.add_field(
            name="📊 Total",
            value=f"{len(guild.channels)} channels",
            inline=True,
        )

        # Optional: list channels in categories
        if categories:
            category_list = []
            for cat in categories[:10]:
                cat_channels = [c for c in cat.channels]
                category_list.append(f"  • **{cat.name}** — {len(cat_channels)} channels")
            if len(categories) > 10:
                category_list.append(f"  ... and {len(categories) - 10} more categories")
            
            embed.add_field(
                name="Categories & Channels",
                value="\n".join(category_list) if category_list else "*(No categories)*",
                inline=False,
            )

        embed.set_footer(text=f"Guild ID: {guild.id}")
        await ctx.reply(embed=embed, mention_author=False)

    # ── Error handlers ───────────────────────────────────────────────────────

    @admin.error
    @role.error
    async def admin_error(self, ctx: commands.Context, error):
        """Handle errors for admin commands."""
        if isinstance(error, commands.NotOwner):
            await ctx.reply(
                "❌ Only the bot owner can use this command.",
                mention_author=False,
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    """Load the cog."""
    await bot.add_cog(AdminCog(bot))
