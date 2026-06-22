"""
cogs/welcome_cog.py
───────────────────
Features
--------
• Welcome channel  — cyberpunk image card (avatar only) + clean embed with all details.
• Goodbye channel  — red embed sent when a member leaves.
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
import sys
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False
    print("[welcome_cog] Pillow not installed — image cards disabled.", file=sys.stderr)

FONTS_DIR = Path("fonts")

def _font(name: str, size: int):
    p = FONTS_DIR / name
    if PIL_OK and p.exists():
        return ImageFont.truetype(str(p), size)
    return ImageFont.load_default() if PIL_OK else None

import db as _db

def _col():
    return _db.get_db()

# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_config(guild_id: int) -> dict:
    doc = await _col().welcome_config.find_one({"guild_id": guild_id})
    return doc or {}

async def _set_field(guild_id: int, **fields) -> None:
    await _col().welcome_config.update_one(
        {"guild_id": guild_id}, {"$set": fields}, upsert=True
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

# ── Image card ────────────────────────────────────────────────────────────────

CARD_W, CARD_H = 950, 280

def _circle_crop(img: "Image.Image", size: int) -> "Image.Image":
    img  = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _build_card_image(
    avatar_bytes: bytes,
    username: str,
    server_name: str,
) -> bytes:
    """Compose the cyberpunk welcome card. Returns PNG bytes."""

    # ── Base dark background ──────────────────────────────────────────────────
    card = Image.new("RGBA", (CARD_W, CARD_H), (8, 6, 18, 255))
    draw = ImageDraw.Draw(card)

    # Faint neon grid
    for y in range(0, CARD_H, 28):
        draw.line([(0, y), (CARD_W, y)], fill=(0, 220, 255, 18), width=1)
    for x in range(0, CARD_W, 40):
        draw.line([(x, 0), (x, CARD_H)], fill=(0, 220, 255, 18), width=1)

    # Diagonal streaks top-right
    for i in range(6):
        off = i * 14
        draw.line([(CARD_W - 180 + off, 0), (CARD_W + off, 200)],
                  fill=(0, 220, 255, 28 - i * 4), width=3)

    # Scan-line overlay
    for y in range(0, CARD_H, 4):
        draw.line([(0, y), (CARD_W, y)], fill=(0, 0, 0, 18), width=1)

    # ── Left avatar slab ──────────────────────────────────────────────────────
    glow = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(
        [18, 18, 230, CARD_H - 18], radius=18, fill=(0, 220, 255, 22)
    )
    card = Image.alpha_composite(card, glow)
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([18, 18, 230, CARD_H - 18], radius=18,
                            outline=(0, 220, 255, 140), width=2)

    # ── Right text panel ──────────────────────────────────────────────────────
    panel = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle(
        [248, 18, CARD_W - 18, CARD_H - 18], radius=18, fill=(10, 8, 28, 200)
    )
    card = Image.alpha_composite(card, panel)
    draw = ImageDraw.Draw(card)

    # Panel borders — magenta top, cyan bottom
    draw.line([(248, 18), (CARD_W - 18, 18)],           fill=(255, 0, 200, 160), width=2)
    draw.line([(248, CARD_H-18), (CARD_W-18, CARD_H-18)], fill=(0, 220, 255, 160), width=2)
    draw.line([(248, 18), (248, CARD_H - 18)],           fill=(80, 0, 120, 120),  width=2)
    draw.line([(CARD_W-18, 18), (CARD_W-18, CARD_H-18)], fill=(80, 0, 120, 120), width=2)

    # ── Avatar ────────────────────────────────────────────────────────────────
    AV_SIZE = 168
    AV_X    = 36
    AV_Y    = (CARD_H - AV_SIZE) // 2

    # Magenta glow rings
    glow_r = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    gr = ImageDraw.Draw(glow_r)
    for i in range(12, 0, -1):
        alpha = int(110 * (i / 12) ** 2)
        gr.ellipse(
            [AV_X - i, AV_Y - i, AV_X + AV_SIZE + i, AV_Y + AV_SIZE + i],
            outline=(255, 0, 200, alpha), width=1,
        )
    card = Image.alpha_composite(card, glow_r)
    draw = ImageDraw.Draw(card)

    draw.ellipse([AV_X-3, AV_Y-3, AV_X+AV_SIZE+3, AV_Y+AV_SIZE+3],
                 outline=(255, 0, 200, 255), width=3)
    draw.ellipse([AV_X+1, AV_Y+1, AV_X+AV_SIZE-1, AV_Y+AV_SIZE-1],
                 outline=(0, 220, 255, 180), width=2)

    try:
        av_img  = Image.open(io.BytesIO(avatar_bytes))
    except Exception:
        av_img  = Image.new("RGBA", (AV_SIZE, AV_SIZE), (60, 30, 100))
    av_circ = _circle_crop(av_img, AV_SIZE)
    card.paste(av_circ, (AV_X, AV_Y), av_circ)

    # ── Text (vertically centred in right panel) ──────────────────────────────
    f_label  = _font("Poppins-Medium.ttf",   15)
    f_server = _font("Poppins-SemiBold.ttf", 20)
    f_name   = _font("Poppins-Bold.ttf",     54)

    TX         = 278
    PANEL_MID  = CARD_H // 2

    label_h  = draw.textbbox((0, 0), "WELCOME TO",       font=f_label)[3]
    server_h = draw.textbbox((0, 0), server_name.upper(), font=f_server)[3]
    name_h   = draw.textbbox((0, 0), username,            font=f_name)[3]
    GAP1, GAP2 = 6, 8
    total_h  = label_h + GAP1 + server_h + GAP2 + name_h
    start_y  = PANEL_MID - total_h // 2

    # "WELCOME TO" — cyan
    draw.text((TX, start_y), "WELCOME TO", font=f_label, fill=(0, 220, 255, 200))

    # Server name — magenta
    srv_y       = start_y + label_h + GAP1
    server_disp = server_name if len(server_name) <= 30 else server_name[:29] + "…"
    draw.text((TX, srv_y), server_disp.upper(), font=f_server, fill=(255, 0, 200))

    # Thin divider
    div_y = srv_y + server_h + 4
    draw.line([(TX, div_y), (CARD_W - 36, div_y)], fill=(0, 220, 255, 60), width=1)

    # Username — white with magenta shadow + cyan underline
    name_y    = div_y + GAP2
    name_disp = username if len(username) <= 16 else username[:15] + "…"
    draw.text((TX + 2, name_y + 2), name_disp, font=f_name, fill=(255, 0, 200, 100))
    draw.text((TX,     name_y),     name_disp, font=f_name, fill=(255, 255, 255))

    nb = draw.textbbox((0, 0), name_disp, font=f_name)
    draw.line(
        [(TX, name_y + nb[3] + 2), (TX + nb[2], name_y + nb[3] + 2)],
        fill=(0, 220, 255, 200), width=2,
    )

    # Corner squares
    sq = 6
    for (cx, cy, col) in [
        (18, 18, (255,0,200)), (CARD_W-18-sq, 18, (0,220,255)),
        (18, CARD_H-18-sq, (0,220,255)), (CARD_W-18-sq, CARD_H-18-sq, (255,0,200)),
    ]:
        draw.rectangle([cx, cy, cx+sq, cy+sq], fill=col)

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()


async def build_welcome_card(
    member: discord.Member,
    guild:  discord.Guild,
) -> Optional[discord.File]:
    """Fetch avatar, build card, return discord.File or None."""
    if not PIL_OK:
        return None

    avatar_url = member.display_avatar.replace(size=256, format="png").url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(avatar_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                avatar_bytes = await resp.read()
    except Exception:
        avatar_bytes = b""

    png = _build_card_image(avatar_bytes, member.display_name, guild.name)
    return discord.File(io.BytesIO(png), filename="welcome.png")


# ── Invite tracker ────────────────────────────────────────────────────────────

class InviteTracker:
    def __init__(self):
        self._cache: dict[int, dict[str, int]] = {}

    async def snapshot(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
            self._cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except discord.Forbidden:
            self._cache[guild.id] = {}

    async def find_inviter(self, guild: discord.Guild) -> Optional[discord.Member]:
        old = self._cache.get(guild.id, {})
        try:
            new_invites = await guild.invites()
        except discord.Forbidden:
            return None
        for inv in new_invites:
            if inv.uses > old.get(inv.code, 0):
                self._cache.setdefault(guild.id, {})[inv.code] = inv.uses
                if inv.inviter:
                    return guild.get_member(inv.inviter.id)
        return None


# ── Cog ───────────────────────────────────────────────────────────────────────

class WelcomeCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot     = bot
        self.tracker = InviteTracker()

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self.tracker.snapshot(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.tracker.snapshot(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            await self.tracker.snapshot(invite.guild)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild  = member.guild
        config = await _get_config(guild.id)

        # 1. Find inviter
        inviter = await self.tracker.find_inviter(guild)

        # 2. Auto-role
        auto_role_id = config.get("auto_role_id")
        if auto_role_id:
            role = guild.get_role(int(auto_role_id))
            if role:
                try:
                    await member.add_roles(role, reason="welcome_cog: auto-role")
                except discord.Forbidden:
                    pass

        # 3. Special role
        special_role_id = config.get("special_roles", {}).get(str(member.id))
        if special_role_id:
            role = guild.get_role(int(special_role_id))
            if role:
                try:
                    await member.add_roles(role, reason="welcome_cog: special role")
                except discord.Forbidden:
                    pass

        # 4. Welcome message
        wc_id = config.get("welcome_channel_id")
        if not wc_id:
            return
        channel = guild.get_channel(int(wc_id))
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Build card
        card_file = await build_welcome_card(member, guild)

        # Build embed (all the details live here, NO pings)
        embed = discord.Embed(colour=discord.Colour(0xFF00C8))
        embed.set_author(
            name=f"Welcome to {guild.name}!",
            icon_url=guild.icon.url if guild.icon else None,
        )

        lines = [
            f"**{member.display_name}** just joined the server.",
            f"You are member **#{guild.member_count}**.",
        ]
        if inviter:
            lines.append(f"Invited by **{inviter.display_name}**.")

        embed.description = "\n".join(lines)
        embed.set_footer(
            text=f"Account created {member.created_at.strftime('%d %b %Y')}",
            icon_url=member.display_avatar.url,
        )

        if card_file:
            embed.set_image(url="attachment://welcome.png")
            await channel.send(embed=embed, file=card_file)
        else:
            await channel.send(embed=embed)

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
                f"We are now **{guild.member_count}** members."
            ),
            colour=discord.Colour(0xED4245),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=member.created_at.strftime("Joined Discord: %d %b %Y"))
        await channel.send(embed=embed)

    # ── Slash commands ────────────────────────────────────────────────────────

    grp = app_commands.Group(
        name="welcome",
        description="Welcome / leave / role configuration",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @grp.command(name="set_welcome_channel",
                 description="Set the channel for welcome messages.")
    @app_commands.describe(channel="Text channel to post welcome messages in.")
    async def set_welcome_channel(self, interaction: discord.Interaction,
                                  channel: discord.TextChannel):
        await _set_field(interaction.guild_id, welcome_channel_id=channel.id)
        await interaction.response.send_message(
            f"✅ Welcome channel set to {channel.mention}.", ephemeral=True)

    @grp.command(name="set_leave_channel",
                 description="Set the channel for goodbye messages.")
    @app_commands.describe(channel="Text channel to post leave messages in.")
    async def set_leave_channel(self, interaction: discord.Interaction,
                                channel: discord.TextChannel):
        await _set_field(interaction.guild_id, leave_channel_id=channel.id)
        await interaction.response.send_message(
            f"✅ Leave channel set to {channel.mention}.", ephemeral=True)

    @grp.command(name="set_auto_role",
                 description="Assign this role to every new member automatically.")
    @app_commands.describe(role="Role to auto-assign on join.")
    async def set_auto_role(self, interaction: discord.Interaction,
                            role: discord.Role):
        await _set_field(interaction.guild_id, auto_role_id=role.id)
        await interaction.response.send_message(
            f"✅ Auto-role set to **{role.name}**.", ephemeral=True)

    @grp.command(name="add_special_role",
                 description="Give a specific role to a specific user when they join.")
    @app_commands.describe(user="The user.", role="Role to give them on join.")
    async def add_special_role(self, interaction: discord.Interaction,
                               user: discord.User, role: discord.Role):
        await _add_special_role(interaction.guild_id, user.id, role.id)
        await interaction.response.send_message(
            f"✅ When **{user}** joins they'll receive **{role.name}**.", ephemeral=True)

    @grp.command(name="remove_special_role",
                 description="Remove the special-join role for a specific user.")
    @app_commands.describe(user="The user to remove the special role for.")
    async def remove_special_role(self, interaction: discord.Interaction,
                                  user: discord.User):
        await _remove_special_role(interaction.guild_id, user.id)
        await interaction.response.send_message(
            f"✅ Removed special role entry for **{user}**.", ephemeral=True)

    @grp.command(name="test_welcome",
                 description="Fire a test welcome message for yourself.")
    async def test_welcome(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.on_member_join(interaction.user)   # type: ignore[arg-type]
        await interaction.followup.send("✅ Test welcome sent!", ephemeral=True)

    @grp.command(name="status",
                 description="Show the current welcome / leave / role config.")
    async def status(self, interaction: discord.Interaction):
        guild  = interaction.guild
        config = await _get_config(guild.id)

        def fmt_ch(cid):
            if not cid: return "*not set*"
            ch = guild.get_channel(int(cid))
            return ch.mention if ch else f"*(deleted — id {cid})*"

        def fmt_role(rid):
            if not rid: return "*not set*"
            r = guild.get_role(int(rid))
            return r.mention if r else f"*(deleted — id {rid})*"

        special = config.get("special_roles", {})
        special_lines = []
        for uid, rid in special.items():
            m = guild.get_member(int(uid))
            r = guild.get_role(int(rid))
            special_lines.append(
                f"• {m.mention if m else f'`{uid}`'} → {r.mention if r else f'`{rid}`'}"
            )

        embed = discord.Embed(title="📋 Welcome Cog — Config", colour=0x7289DA)
        embed.add_field(name="Welcome Channel", value=fmt_ch(config.get("welcome_channel_id")), inline=False)
        embed.add_field(name="Leave Channel",   value=fmt_ch(config.get("leave_channel_id")),   inline=False)
        embed.add_field(name="Auto-Role",       value=fmt_role(config.get("auto_role_id")),     inline=False)
        embed.add_field(name="Special Roles",
                        value="\n".join(special_lines) if special_lines else "*none*",
                        inline=False)
        embed.add_field(name="Image Cards",
                        value="✅ Pillow + fonts ready" if PIL_OK else "⚠️ Pillow not installed",
                        inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
