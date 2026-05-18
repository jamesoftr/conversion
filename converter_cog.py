import json
import re
import discord
from discord import app_commands
from discord.ext import commands
from collections import OrderedDict

# Custom emoji that replaces any custom emoji in converted content
# (the bot cannot copy custom emojis from the original server)
_REPLACEMENT_EMOJI = "<:reply:1503236369126916117>"

# Matches custom/animated Discord emojis: <:name:id> or <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")


def _replace_custom_emojis(text: str) -> str:
    """Replace every custom Discord emoji in *text* with the replacement emoji."""
    return _CUSTOM_EMOJI_RE.sub(_REPLACEMENT_EMOJI, text)

CACHE_MAX_SIZE = 500

AUTO_CONVERT_BOT_IDS = [
    # 000000000000000000,  # Pokétwo
]

_converted: OrderedDict[int, bool] = OrderedDict()
_CONVERTED_MAX = 1000

# Maps original message_id -> channel_id of the webhook reply, so we can
# label edit conversions with a jump link back to the original.
_edit_origin: OrderedDict[int, int] = OrderedDict()
_EDIT_ORIGIN_MAX = 1000


def _store_edit_origin(message_id: int, channel_id: int):
    if message_id not in _edit_origin:
        if len(_edit_origin) >= _EDIT_ORIGIN_MAX:
            _edit_origin.popitem(last=False)
    _edit_origin[message_id] = channel_id

_author_cache: OrderedDict[int, dict] = OrderedDict()
_AUTHOR_CACHE_MAX = 2000


def _store_author(message_id: int, author: dict):
    if message_id not in _author_cache:
        if len(_author_cache) >= _AUTHOR_CACHE_MAX:
            _author_cache.popitem(last=False)
    _author_cache[message_id] = author


def _get_author(message_id: int, payload_author: dict) -> dict:
    if payload_author:
        return payload_author
    return _author_cache.get(message_id, {})


def _mark_converted(message_id: int):
    if message_id not in _converted:
        if len(_converted) >= _CONVERTED_MAX:
            _converted.popitem(last=False)
        _converted[message_id] = True


def _unmark_converted(message_id: int):
    _converted.pop(message_id, None)


def _already_converted(message_id: int) -> bool:
    return message_id in _converted


# Component type reference (Discord API):
#  1  = ActionRow
#  2  = Button
#  3  = StringSelect
#  9  = Section        ← has "components" (children) + optional "accessory"
#  10 = TextDisplay
#  11 = Thumbnail      ← has "media" + optional "description"
#  12 = MediaGallery   ← has "items"
#  13 = File
#  14 = Separator
#  17 = Container
#  25 = Section (older name — same structure, keep both)

def _walk(components: list, result: dict):
    for comp in components:
        ctype = comp.get("type")

        if ctype == 17:        # Container
            if result["color"] is None and comp.get("accent_color") is not None:
                result["color"] = comp["accent_color"]
            _walk(comp.get("components", []), result)

        elif ctype == 1:       # ActionRow
            _walk(comp.get("components", []), result)

        elif ctype in (9, 25): # Section — children in "components", button in "accessory"
            _walk(comp.get("components", []), result)
            accessory = comp.get("accessory")
            if accessory:
                _walk([accessory], result)

        elif ctype == 10:      # TextDisplay
            content = comp.get("content", "").strip()
            if content:
                result["text"].append(_replace_custom_emojis(content))

        elif ctype == 11:      # Thumbnail (correct type number)
            url = (comp.get("media") or {}).get("url", "")
            desc = comp.get("description", "")
            if url and result["thumbnail"] is None:
                result["thumbnail"] = (url, desc)

        elif ctype == 14:      # Separator
            pass

        elif ctype == 12:      # MediaGallery
            for item in comp.get("items", []):
                url = (item.get("media") or {}).get("url", "")
                desc = item.get("description", "")
                if url:
                    result["images"].append((url, desc))

        elif ctype == 13:      # File
            f = comp.get("file", {})
            url = f.get("url", "")
            name = f.get("filename", "file")
            if url:
                result["text"].append(f"📎 [{name}]({url})")

        elif ctype == 2:       # Button
            label = comp.get("label", "").strip()
            url   = comp.get("url")
            # Emoji object may be a custom emoji (unusable by the bot) — drop it entirely.
            # Only keep standard Unicode emoji if it's not a custom one.
            emoji_obj = comp.get("emoji") or {}
            emoji_id  = emoji_obj.get("id")   # present for custom emoji, None for unicode
            emoji_name = emoji_obj.get("name", "")
            if emoji_id is None and emoji_name:
                # Standard unicode emoji — safe to prepend
                display = f"{emoji_name} {label}".strip()
            else:
                # Custom emoji — omit it, just use the label
                display = label
            if display:
                result["buttons"].append((display, url))

        elif ctype == 3:       # StringSelect
            placeholder = comp.get("placeholder", "Select an option")
            options     = [o.get("label", "") for o in comp.get("options", [])]
            result["selects"].append((placeholder, options))

        else:
            # Unknown — recurse defensively
            _walk(comp.get("components", []), result)
            accessory = comp.get("accessory")
            if accessory:
                _walk([accessory], result)


def _parse(raw_components: list) -> dict:
    result = {"text": [], "buttons": [], "selects": [],
              "images": [], "thumbnail": None, "color": None}
    _walk(raw_components, result)
    return result


def _build_view(data: dict) -> discord.ui.View | None:
    """Build a discord.ui.View with real buttons and selects from parsed data.

    Buttons with a URL become link buttons (always enabled, no callback needed).
    Buttons without a URL become disabled grey buttons (non-functional, display only).
    Selects become disabled dropdowns showing the original placeholder + options.
    Returns None when there are no interactive components.
    """
    if not data["buttons"] and not data["selects"]:
        return None

    view = discord.ui.View(timeout=None)

    # Add buttons (max 25 total across all rows; Discord allows 5 per ActionRow)
    for label, url in data["buttons"][:25]:
        if url:
            btn = discord.ui.Button(
                label=label[:80],
                url=url,
                style=discord.ButtonStyle.link,
            )
        else:
            btn = discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.secondary,
                disabled=True,
            )
        view.add_item(btn)

    # Add selects (max 5; each takes a full row)
    for ph, opts in data["selects"][:5]:
        options = [
            discord.SelectOption(label=o[:100] or "\u2014", value=o[:100] or "__none__")
            for o in opts[:25]
        ] or [discord.SelectOption(label="(no options)", value="__none__")]

        select = discord.ui.Select(
            placeholder=ph[:150],
            options=options,
            disabled=True,
        )
        view.add_item(select)

    return view


def _build_message(message: discord.Message, raw_components: list) -> tuple[list[discord.Embed], discord.ui.View | None]:
    """Return (embeds, view) for the converted message."""
    data = _parse(raw_components)

    if message.content and message.content.strip():
        data["text"].insert(0, _replace_custom_emojis(message.content.strip()))

    for embed in message.embeds:
        if embed.description:
            data["text"].append(_replace_custom_emojis(embed.description))
        for field in embed.fields:
            data["text"].append(f"**{field.name}**\n{_replace_custom_emojis(field.value)}")
        if embed.image and embed.image.url:
            data["images"].append((embed.image.url, ""))
        if embed.thumbnail and embed.thumbnail.url and data["thumbnail"] is None:
            data["thumbnail"] = (embed.thumbnail.url, "")
        if embed.color and data["color"] is None:
            data["color"] = embed.color.value

    for att in message.attachments:
        if (att.content_type or "").startswith("image/"):
            data["images"].append((att.url, att.filename))
        else:
            data["text"].append(f"\U0001f4ce [{att.filename}]({att.url})")

    color = discord.Color(data["color"]) if data["color"] else discord.Color.blurple()

    desc_parts = list(data["text"])

    # Consider it readable if there's text OR at least one image
    has_content = bool(desc_parts) or bool(data["images"]) or data["thumbnail"] is not None
    description = "\n\n".join(desc_parts) if desc_parts else "*No text content.*"
    if len(description) > 4090:
        description = description[:4087] + "\u2026"

    main = discord.Embed(description=description, color=color, timestamp=message.created_at)
    main.set_footer(text=f"#{message.channel.name}  \u2022  Components V2 \u2192 Embed")

    if data["thumbnail"]:
        main.set_thumbnail(url=data["thumbnail"][0])
    if data["images"]:
        main.set_image(url=data["images"][0][0])

    embeds = [main]
    for url, desc in data["images"][1:9]:
        extra = discord.Embed(color=color)
        extra.set_image(url=url)
        if desc:
            extra.description = f"*{desc}*"
        embeds.append(extra)

    if not has_content:
        return [], None

    view = _build_view(data)
    return embeds, view


# Thin shim for any callers that only need embeds
def _build_embeds(message: discord.Message, raw_components: list) -> list[discord.Embed]:
    embeds, _ = _build_message(message, raw_components)
    return embeds


async def _get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.name == "V2 Converter Pro":
            return wh
    return await channel.create_webhook(name="V2 Converter Pro")


async def _send_via_webhook(
    message: discord.Message,
    embeds: list[discord.Embed],
    *,
    view: discord.ui.View | None = None,
    edited: bool = False,
):
    channel = message.channel
    content = f"✏️ *(this message was edited — [jump to original]({message.jump_url}))*" if edited else None
    # Webhooks don't support view= directly; send the view as a separate bot message.
    if isinstance(channel, discord.Thread):
        wh = await _get_or_create_webhook(channel.parent)
        await wh.send(
            content=content,
            embeds=embeds,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
            thread=channel,
        )
        if view is not None:
            await channel.send(view=view)
    elif isinstance(channel, discord.TextChannel):
        wh = await _get_or_create_webhook(channel)
        await wh.send(
            content=content,
            embeds=embeds,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
        )
        if view is not None:
            await channel.send(view=view)
    else:
        await channel.send(content=content, embeds=embeds, view=view)


def _is_v2_message(payload: dict, components: list) -> bool:
    has_container = any(c.get("type") == 17 for c in components)
    flag_v2       = bool(payload.get("flags", 0) & (1 << 15))
    return (has_container or flag_v2) and bool(components)


def _summarise_components(components: list, indent: int = 0) -> str:
    lines = []
    pad = "  " * indent
    for comp in components:
        ctype = comp.get("type", "?")
        label = comp.get("content") or comp.get("label") or comp.get("placeholder") or ""
        if label:
            label = f' "{label[:50]}"'
        lines.append(f"{pad}type={ctype}{label}")
        children = comp.get("components", [])
        if children:
            lines.append(_summarise_components(children, indent + 1))
        accessory = comp.get("accessory")
        if accessory:
            lines.append(f"{pad}  [accessory]")
            lines.append(_summarise_components([accessory], indent + 2))
        for item in comp.get("items", []):
            url = (item.get("media") or {}).get("url", "")
            lines.append(f"{pad}  item url={url[:80]}")
    return "\n".join(lines)


class ConverterCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: OrderedDict[int, list] = OrderedDict()
        # guild_id → set of allowed channel IDs (loaded from DB on first use)
        self._allowed_channels: dict[int, set[int]] = {}

    # ── Allowed-channels helpers ──────────────────────────────────────────────

    async def _load_allowed(self, guild_id: int) -> set[int]:
        """Load allowed channels from DB into cache and return the set."""
        import db
        doc = await db.get_db().converter_channels.find_one({"guild_id": guild_id})
        channels: set[int] = set(doc.get("channel_ids", [])) if doc else set()
        self._allowed_channels[guild_id] = channels
        return channels

    async def _get_allowed(self, guild_id: int) -> set[int]:
        """Return cached allowed channels, loading from DB if not yet cached."""
        if guild_id not in self._allowed_channels:
            return await self._load_allowed(guild_id)
        return self._allowed_channels[guild_id]

    async def _save_allowed(self, guild_id: int, channels: set[int]) -> None:
        """Persist allowed channels to DB and update cache."""
        import db
        await db.get_db().converter_channels.update_one(
            {"guild_id": guild_id},
            {"$set": {"channel_ids": list(channels)}},
            upsert=True,
        )
        self._allowed_channels[guild_id] = channels

    async def _is_channel_allowed(self, guild_id: int, channel_id: int) -> bool:
        """Return True if no restrictions set (allow all) or channel is in the list."""
        allowed = await self._get_allowed(guild_id)
        return len(allowed) == 0 or channel_id in allowed

    def _store(self, message_id: int, components: list):
        if message_id in self._cache:
            self._cache.move_to_end(message_id)
        else:
            if len(self._cache) >= CACHE_MAX_SIZE:
                self._cache.popitem(last=False)
            self._cache[message_id] = components

    def _get(self, message_id: int) -> list | None:
        return self._cache.get(message_id)

    async def _handle_raw_event(self, event_type: str, payload: dict):
        message_id = int(payload.get("id", 0))
        if not message_id:
            return

        components = payload.get("components", [])
        flags      = payload.get("flags", 0)
        raw_author = payload.get("author", {})

        if raw_author:
            _store_author(message_id, raw_author)

        author    = _get_author(message_id, raw_author)
        author_id = int(author.get("id", 0))
        is_bot    = author.get("bot", False)

        has_container = any(c.get("type") == 17 for c in components)
        flag_v2       = bool(flags & (1 << 15))
        is_v2         = _is_v2_message(payload, components)

        print(
            f"\n{'='*60}\n"
            f"[DBG] {event_type} | msg={message_id}\n"
            f"  author_id={author_id} is_bot={is_bot} "
            f"(in_payload={bool(raw_author)} from_cache={not bool(raw_author) and bool(author)})\n"
            f"  flags={flags} | flag_v2(bit15)={flag_v2} | has_container(t17)={has_container} | is_v2={is_v2}\n"
            f"  components({len(components)}): {[c.get('type') for c in components]}\n"
            f"  already_converted={_already_converted(message_id)}"
        )

        if components:
            tree = _summarise_components(components)
            print(f"  component tree:\n{tree}")

        if not is_v2:
            print(f"  → SKIP: not a V2 message (flag_v2={flag_v2}, has_container={has_container})")
            return

        if components:
            self._store(message_id, components)
            print(f"  → cached {len(components)} top-level component(s)")

        if not is_bot:
            print(f"  → SKIP: author is not a bot (or author unknown from payload and cache)")
            return

        if AUTO_CONVERT_BOT_IDS and author_id not in AUTO_CONVERT_BOT_IDS:
            print(f"  → SKIP: bot {author_id} not in AUTO_CONVERT_BOT_IDS={AUTO_CONVERT_BOT_IDS}")
            return

        if not components:
            print(f"  → SKIP: components list is empty")
            return

        is_edit = event_type == "MESSAGE_UPDATE"

        if _already_converted(message_id):
            if is_edit:
                # Allow re-conversion for edits
                print(f"  → EDIT detected: clearing converted flag for re-conversion")
                _unmark_converted(message_id)
            else:
                print(f"  → SKIP: already converted this message")
                return

        channel_id = int(payload.get("channel_id", 0))
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            print(f"  → SKIP: channel {channel_id} not in bot cache")
            return

        guild_id = int(payload.get("guild_id", 0))
        if guild_id and not await self._is_channel_allowed(guild_id, channel_id):
            print(f"  → SKIP: channel {channel_id} not in allowed list for guild {guild_id}")
            return

        try:
            message = await channel.fetch_message(message_id)
        except Exception as e:
            print(f"  → SKIP: fetch_message failed: {type(e).__name__}: {e}")
            return

        parsed = _parse(components)
        print(
            f"  parsed → text={len(parsed['text'])} buttons={len(parsed['buttons'])} "
            f"images={len(parsed['images'])} thumbnail={parsed['thumbnail'] is not None}"
        )

        embeds, view = _build_message(message, components)
        if not embeds:
            print(f"  → SKIP: _build_message produced no content (no text, images, or thumbnail)")
            return

        action = "EDITING" if is_edit else "CONVERTING"
        print(f"  → ✅ {action}: sending {len(embeds)} embed(s) + view={view is not None} via webhook")
        _mark_converted(message_id)
        await _send_via_webhook(message, embeds, view=view, edited=is_edit)

    @commands.Cog.listener()
    async def on_socket_raw_receive(self, raw: str | bytes):
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            if data.get("op") != 0:
                return

            event_type = data.get("t")
            if event_type not in ("MESSAGE_CREATE", "MESSAGE_UPDATE"):
                return

            payload = data.get("d", {})
            await self._handle_raw_event(event_type, payload)

        except Exception as e:
            print(f"[AUTO-CONVERT ERROR] {type(e).__name__}: {e}")

    # ── Manual !convert command ───────────────────────────────────────────────

    async def _do_convert(self, target: discord.Message) -> tuple[str, list[discord.Embed], discord.ui.View | None]:
        raw = self._get(target.id)
        note = "-# ✅ Source: Components V2 (live cache)" if raw else \
               "-# ⚠️ Not in cache — message may have been sent before bot started."
        embeds, view = _build_message(target, raw or [])
        intro  = (
            f"📬 **Converted** from {target.author.mention} — "
            f"[Jump to original]({target.jump_url})\n{note}"
        )
        return intro, embeds, view

    @commands.command(name="convert", help="Reply to a Components V2 message to convert it.")
    async def prefix_convert(self, ctx: commands.Context):
        if ctx.message.reference is None:
            await ctx.reply("❌ Please **reply** to the message you want to convert.")
            return
        try:
            ref    = ctx.message.reference
            target = ref.resolved or await ctx.channel.fetch_message(ref.message_id)
        except discord.NotFound:
            await ctx.reply("❌ Could not fetch that message.")
            return
        try:
            intro, embeds, view = await self._do_convert(target)
            await ctx.reply(content=intro, embeds=embeds, view=view)
        except Exception as e:
            await ctx.reply(f"❌ Error:\n```\n{type(e).__name__}: {e}\n```")

    @app_commands.command(name="convert", description="Convert a Components V2 message to a classic embed.")
    async def slash_convert(self, interaction: discord.Interaction):
        ref = interaction.message.reference if interaction.message else None
        if ref is None:
            await interaction.response.send_message(
                "❌ Reply to a Components V2 message first, then run this command.",
                ephemeral=True,
            )
            return
        try:
            target = ref.resolved or await interaction.channel.fetch_message(ref.message_id)
        except discord.NotFound:
            await interaction.response.send_message("❌ Could not fetch that message.", ephemeral=True)
            return
        try:
            await interaction.response.defer()
            intro, embeds, view = await self._do_convert(target)
            await interaction.followup.send(content=intro, embeds=embeds, view=view)
        except Exception as e:
            await interaction.followup.send(f"❌ Error:\n```\n{type(e).__name__}: {e}\n```")


    # ── Channel restriction commands ──────────────────────────────────────────

    @commands.group(name="convertch", aliases=["cch"], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def convertch(self, ctx: commands.Context):
        """Manage which channels auto-conversion is allowed in."""
        await ctx.send_help(ctx.command)

    @convertch.command(name="list")
    @commands.has_permissions(manage_guild=True)
    async def cch_list(self, ctx: commands.Context):
        """Show allowed channels. Empty = conversion allowed everywhere."""
        allowed = await self._get_allowed(ctx.guild.id)
        if not allowed:
            await ctx.reply("📋 No channel restrictions set — conversion runs in **all channels**.")
            return
        mentions = []
        for cid in sorted(allowed):
            ch = ctx.guild.get_channel(cid)
            mentions.append(ch.mention if ch else f"`{cid}` *(deleted)*")
        e = discord.Embed(
            title="📋 Converter Allowed Channels",
            description="\n".join(mentions),
            color=discord.Color.blurple(),
        )
        e.set_footer(text=f"{len(allowed)} channel(s) — conversion restricted to these only")
        await ctx.reply(embed=e)

    @convertch.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def cch_add(self, ctx: commands.Context, *channels: discord.TextChannel):
        """Add one or more channels to the allowed list.
        Usage: a!convertch add #chan1 #chan2"""
        if not channels:
            await ctx.reply("❌ Please mention at least one channel.")
            return
        allowed = await self._get_allowed(ctx.guild.id)
        added = []
        already = []
        for ch in channels:
            if ch.id in allowed:
                already.append(ch.mention)
            else:
                allowed.add(ch.id)
                added.append(ch.mention)
        await self._save_allowed(ctx.guild.id, allowed)
        parts = []
        if added:
            parts.append(f"✅ Added: {', '.join(added)}")
        if already:
            parts.append(f"ℹ️ Already in list: {', '.join(already)}")
        await ctx.reply("\n".join(parts))

    @convertch.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def cch_remove(self, ctx: commands.Context, *channels: discord.TextChannel):
        """Remove one or more channels from the allowed list.
        Usage: a!convertch remove #chan1 #chan2"""
        if not channels:
            await ctx.reply("❌ Please mention at least one channel.")
            return
        allowed = await self._get_allowed(ctx.guild.id)
        removed = []
        missing = []
        for ch in channels:
            if ch.id in allowed:
                allowed.discard(ch.id)
                removed.append(ch.mention)
            else:
                missing.append(ch.mention)
        await self._save_allowed(ctx.guild.id, allowed)
        parts = []
        if removed:
            parts.append(f"✅ Removed: {', '.join(removed)}")
        if missing:
            parts.append(f"ℹ️ Not in list: {', '.join(missing)}")
        if not allowed:
            parts.append("⚠️ List is now empty — conversion will run in **all channels**.")
        await ctx.reply("\n".join(parts))

    @convertch.command(name="clear")
    @commands.has_permissions(manage_guild=True)
    async def cch_clear(self, ctx: commands.Context):
        """Clear all channel restrictions — conversion will run everywhere again."""
        await self._save_allowed(ctx.guild.id, set())
        await ctx.reply("🗑️ Channel restrictions cleared — conversion is now allowed in **all channels**.")

    @convertch.error
    async def cch_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You need **Manage Guild** permission to use this.")
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(ConverterCog(bot))
