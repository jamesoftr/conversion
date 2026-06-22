"""
cogs/welcome_cog.py
───────────────────
Features
--------
• Welcome channel  — rich image card (pfp + background + "Invited by") sent when a member joins.
• Goodbye channel  — text embed sent when a member leaves.
• Auto-role        — one role automatically assigned to every new member.
• Special roles    — map specific user IDs → role IDs; role is granted on join.
• Invite tracking  — tracks which invite link was used so "Invited by" works.

Slash commands (all require Manage Guild)
-----------------------------------------
/welcome set_welcome_channel  #channel
/welcome set_leave_channel    #channel
/welcome set_auto_role        @role
/welcome add_special_role     user  @role
/welcome remove_special_role  user
/welcome test_welcome         (fires a fake welcome for yourself)
/welcome status               (shows current config)
"""

import asyncio
import io
import os
import sys
import textwrap
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ── Pillow (graceful import) ───────────────────────────────────────────────────
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False
    print("[welcome_cog] Pillow not installed — image cards disabled. "
          "Run: pip install Pillow", file=sys.stderr)

# ── Font paths (downloaded at bot startup) ────────────────────────────────────
FONTS_DIR = Path("fonts")

def _font(name: str, size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    """Load a Poppins font by filename. Falls back to default if missing."""
    path = FONTS_DIR / name
    if PIL_OK and path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default() if PIL_OK else None


# ── DB helpers (imported lazily to avoid circular import at module level) ──────
import db as _db

def _col():
    return _db.get_db()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Database helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _get_config(guild_id: int) -> dict:
    doc = await _col().welcome_config.find_one({"guild_id": guild_id})
    return doc or {}


async def _set_field(guild_id: int, **fields) -> None:
    await _col().welcome_config.update_one(
        {"guild_id": guild_id},
        {"$set": fields},
        upsert=True,
    )


async def _add_special_role(guild_id: int, user_id: int, role_id: int) -> None:
    await _col().welcome_config.update_one(
        {"guild_id": guild_id},
        {"$set": {f"special_roles.{user_id}": role_id}},
        upsert=True,
    )


async def _remove_special_role(guild_id: int, user_id: int) -> None:
    await _col().welcome_config.update_one(
        {"guild_id": guild_id},
        {"$unset": {f"special_roles.{user_id}": ""}},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Image generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CARD_W, CARD_H = 900, 300
AVATAR_SIZE    = 160
AVATAR_X, AVATAR_Y = 60, 70

# Gradient colours — feel free to tweak
BG_TOP    = (15,  17,  35)
BG_BOTTOM = (30,  40,  90)
ACCENT    = (114, 137, 218)   # Discord blurple
WHITE     = (255, 255, 255)
SUBTEXT   = (180, 190, 220)


def _make_gradient(w: int, h: int) -> Image.Image:
    """Simple vertical gradient background."""
    base = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t   = y / h
        r   = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g   = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b   = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return base


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
    """Resize + circle-crop an avatar image."""
    img  = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _ring(size: int, colour: tuple, thickness: int = 5) -> Image.Image:
    """Coloured ring to frame the avatar."""
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size - 1, size - 1),
                 outline=colour + (255,), width=thickness)
    return img


def _draw_rounded_rect(draw, xy, radius: int, fill):
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)


async def build_welcome_card(
    member: discord.Member,
    guild:  discord.Guild,
    inviter: Optional[discord.Member],
) -> discord.File | None:
    """Compose the welcome image and return a discord.File, or None if Pillow missing."""
    if not PIL_OK:
        return None

    # ── Fetch avatar bytes ─────────────────────────────────────────────────────
    avatar_url = member.display_avatar.replace(size=256, format="png").url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(avatar_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                avatar_bytes = await resp.read()
        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception:
        avatar_img = Image.new("RGBA", (256, 256), (100, 100, 100, 255))

    # ── Background ─────────────────────────────────────────────────────────────
    card = _make_gradient(CARD_W, CARD_H)

    # Subtle noise / star dots
    import random
    rng  = random.Random(guild.id)
    draw = ImageDraw.Draw(card)
    for _ in range(120):
        sx = rng.randint(0, CARD_W)
        sy = rng.randint(0, CARD_H)
        alpha = rng.randint(80, 200)
        r     = rng.randint(0, 1)
        draw.ellipse([sx, sy, sx + r, sy + r], fill=(255, 255, 255))

    card = card.convert("RGBA")

    # ── Frosted glass panel (right side text area) ────────────────────────────
    panel = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel)
    _draw_rounded_rect(panel_draw, (260, 30, CARD_W - 30, CARD_H - 30),
                       radius=20, fill=(255, 255, 255, 25))
    card = Image.alpha_composite(card, panel)

    # ── Avatar ring + circle ──────────────────────────────────────────────────
    ring_size = AVATAR_SIZE + 12
    ring_img  = _ring(ring_size, ACCENT, thickness=5)
    avatar_c  = _circle_crop(avatar_img, AVATAR_SIZE)

    card.paste(ring_img,  (AVATAR_X - 6, AVATAR_Y - 6), ring_img)
    card.paste(avatar_c,  (AVATAR_X,     AVATAR_Y),      avatar_c)

    # ── Typography ─────────────────────────────────────────────────────────────
    draw = ImageDraw.Draw(card)

    font_big    = _font("Poppins-Bold.ttf",        46)
    font_med    = _font("Poppins-SemiBold.ttf",    24)
    font_small  = _font("Poppins-Regular.ttf",     18)
    font_tiny   = _font("Poppins-MediumItalic.ttf", 15)

    TEXT_X = 280

    # "WELCOME TO" header
    draw.text((TEXT_X, 50), "WELCOME TO", font=font_small, fill=SUBTEXT)

    # Server name
    server_name = guild.name
    if len(server_name) > 22:
        server_name = server_name[:21] + "…"
    draw.text((TEXT_X, 74), server_name.upper(), font=font_med, fill=ACCENT)

    # Member display name  (big)
    username = member.display_name
    if len(username) > 18:
        username = username[:17] + "…"
    draw.text((TEXT_X, 110), username, font=font_big, fill=WHITE)

    # @tag in smaller sub-text
    draw.text((TEXT_X, 164), f"@{member.name}", font=font_small, fill=SUBTEXT)

    # Member count pill
    count_txt = f"🎉  You are member #{guild.member_count}"
    draw.text((TEXT_X, 198), count_txt, font=font_small, fill=(200, 210, 255))

    # Invited by
    if inviter:
        inv_txt = f"✉  Invited by {inviter.display_name}"
        draw.text((TEXT_X, 228), inv_txt, font=font_tiny, fill=(160, 180, 220))

    # Bottom decorative line
    draw.line([(TEXT_X, 260), (CARD_W - 40, 260)], fill=ACCENT + (100,), width=1)

    # Account age footer
    created = member.created_at.strftime("%d %b %Y")
    draw.text((TEXT_X, 267), f"Account created: {created}",
              font=font_tiny, fill=(120, 130, 160))

    # ── Export ─────────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return discord.File(buf, filename="welcome.png")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Invite-usage tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class InviteTracker:
    """Keeps a snapshot of invite use-counts so we can diff on member join."""

    def __init__(self):
        # guild_id → {code: uses}
        self._cache: dict[int, dict[str, int]] = {}

    async def snapshot(self, guild: discord.Guild) -> None:
        """Fetch current invite state and cache it."""
        try:
            invites = await guild.invites()
            self._cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except discord.Forbidden:
            self._cache[guild.id] = {}

    async def find_inviter(
        self, guild: discord.Guild
    ) -> Optional[discord.Member]:
        """
        Compare current invite counts vs cached snapshot.
        Returns the inviter member if found.
        """
        old = self._cache.get(guild.id, {})
        try:
            new_invites = await guild.invites()
        except discord.Forbidden:
            return None

        for inv in new_invites:
            old_uses = old.get(inv.code, 0)
            if inv.uses > old_uses:
                # Update cache entry
                self._cache.setdefault(guild.id, {})[inv.code] = inv.uses
                if inv.inviter:
                    return guild.get_member(inv.inviter.id)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cog
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class WelcomeCog(commands.Cog):
    """Welcome / goodbye / auto-role management."""

    def __init__(self, bot: commands.Bot):
        self.bot     = bot
        self.tracker = InviteTracker()

    # ── Startup: snapshot all guilds ──────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self.tracker.snapshot(guild)

    # ── New guild: snapshot invites ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.tracker.snapshot(guild)

    # ── Someone created an invite: update snapshot ────────────────────────────

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            await self.tracker.snapshot(invite.guild)

    # ── Member join ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild  = member.guild
        config = await _get_config(guild.id)

        # ── 1. Find who invited them ──────────────────────────────────────────
        inviter = await self.tracker.find_inviter(guild)

        # ── 2. Auto-role ──────────────────────────────────────────────────────
        auto_role_id = config.get("auto_role_id")
        if auto_role_id:
            role = guild.get_role(int(auto_role_id))
            if role:
                try:
                    await member.add_roles(role, reason="welcome_cog: auto-role")
                except discord.Forbidden:
                    pass

        # ── 3. Special role ───────────────────────────────────────────────────
        special_roles: dict = config.get("special_roles", {})
        special_role_id = special_roles.get(str(member.id))
        if special_role_id:
            role = guild.get_role(int(special_role_id))
            if role:
                try:
                    await member.add_roles(role, reason="welcome_cog: special role")
                except discord.Forbidden:
                    pass

        # ── 4. Welcome channel ────────────────────────────────────────────────
        wc_id = config.get("welcome_channel_id")
        if not wc_id:
            return
        channel = guild.get_channel(int(wc_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Build embed
        embed = discord.Embed(
            description=(
                f"Hey {member.mention}, welcome to **{guild.name}**! 🎉\n"
                f"You are our **#{guild.member_count}** member.\n"
                + (f"Invited by **{inviter.mention}**" if inviter else "")
            ),
            colour=discord.Colour(0x7289DA),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Joined • {discord.utils.utcnow().strftime('%d %b %Y')}")

        # Build image card
        card_file = await build_welcome_card(member, guild, inviter)

        if card_file:
            embed.set_image(url="attachment://welcome.png")
            await channel.send(
                content=member.mention,
                embed=embed,
                file=card_file,
            )
        else:
            await channel.send(content=member.mention, embed=embed)

    # ── Member leave ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild  = member.guild
        config = await _get_config(guild.id)

        lc_id = config.get("leave_channel_id")
        if not lc_id:
            return
        channel = guild.get_channel(int(lc_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title="👋  Someone Left",
            description=(
                f"**{member.display_name}** (`{member.name}`) just left the server.\n"
                f"We're now **{guild.member_count}** members."
            ),
            colour=discord.Colour(0xED4245),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Left • {discord.utils.utcnow().strftime('%d %b %Y')}")
        await channel.send(embed=embed)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Slash commands
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    grp = app_commands.Group(
        name="welcome",
        description="Welcome / leave / role configuration",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # /welcome set_welcome_channel
    @grp.command(name="set_welcome_channel",
                 description="Set the channel for welcome messages.")
    @app_commands.describe(channel="The text channel to send welcome messages in.")
    async def set_welcome_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        await _set_field(interaction.guild_id, welcome_channel_id=channel.id)
        await interaction.response.send_message(
            f"✅ Welcome channel set to {channel.mention}.", ephemeral=True
        )

    # /welcome set_leave_channel
    @grp.command(name="set_leave_channel",
                 description="Set the channel for goodbye messages.")
    @app_commands.describe(channel="The text channel to send leave messages in.")
    async def set_leave_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        await _set_field(interaction.guild_id, leave_channel_id=channel.id)
        await interaction.response.send_message(
            f"✅ Leave channel set to {channel.mention}.", ephemeral=True
        )

    # /welcome set_auto_role
    @grp.command(name="set_auto_role",
                 description="Assign this role to every new member automatically.")
    @app_commands.describe(role="Role to auto-assign on join.")
    async def set_auto_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ):
        await _set_field(interaction.guild_id, auto_role_id=role.id)
        await interaction.response.send_message(
            f"✅ Auto-role set to **{role.name}**.", ephemeral=True
        )

    # /welcome add_special_role
    @grp.command(name="add_special_role",
                 description="Give a specific role to a specific user when they join.")
    @app_commands.describe(
        user="The Discord user (by ID or mention).",
        role="The role to give them on join.",
    )
    async def add_special_role(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        role: discord.Role,
    ):
        await _add_special_role(interaction.guild_id, user.id, role.id)
        await interaction.response.send_message(
            f"✅ When **{user}** joins they'll receive **{role.name}**.",
            ephemeral=True,
        )

    # /welcome remove_special_role
    @grp.command(name="remove_special_role",
                 description="Remove the special-join role for a specific user.")
    @app_commands.describe(user="The user to remove the special role for.")
    async def remove_special_role(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ):
        await _remove_special_role(interaction.guild_id, user.id)
        await interaction.response.send_message(
            f"✅ Removed special role entry for **{user}**.", ephemeral=True
        )

    # /welcome test_welcome
    @grp.command(name="test_welcome",
                 description="Fire a test welcome message for yourself.")
    async def test_welcome(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.on_member_join(interaction.user)   # type: ignore[arg-type]
        await interaction.followup.send("✅ Test welcome sent!", ephemeral=True)

    # /welcome status
    @grp.command(name="status",
                 description="Show the current welcome / leave / role configuration.")
    async def status(self, interaction: discord.Interaction):
        guild  = interaction.guild
        config = await _get_config(guild.id)

        def fmt_ch(cid):
            if not cid:
                return "*not set*"
            ch = guild.get_channel(int(cid))
            return ch.mention if ch else f"*(deleted — id {cid})*"

        def fmt_role(rid):
            if not rid:
                return "*not set*"
            r = guild.get_role(int(rid))
            return r.mention if r else f"*(deleted — id {rid})*"

        special = config.get("special_roles", {})
        special_lines = []
        for uid, rid in special.items():
            member = guild.get_member(int(uid))
            role   = guild.get_role(int(rid))
            u_str  = member.mention if member else f"`{uid}`"
            r_str  = role.mention   if role   else f"`{rid}`"
            special_lines.append(f"  • {u_str} → {r_str}")

        embed = discord.Embed(title="📋 Welcome Cog — Config", colour=0x7289DA)
        embed.add_field(name="Welcome Channel",
                        value=fmt_ch(config.get("welcome_channel_id")), inline=False)
        embed.add_field(name="Leave Channel",
                        value=fmt_ch(config.get("leave_channel_id")),   inline=False)
        embed.add_field(name="Auto-Role",
                        value=fmt_role(config.get("auto_role_id")),     inline=False)
        embed.add_field(
            name="Special Roles",
            value="\n".join(special_lines) if special_lines else "*none*",
            inline=False,
        )
        embed.add_field(
            name="Image Cards",
            value="✅ Enabled (Pillow + fonts)" if PIL_OK else
                  "⚠️ Disabled — install Pillow",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Setup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
