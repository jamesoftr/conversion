import json
import discord
from discord import app_commands
from discord.ext import commands
from collections import OrderedDict

CACHE_MAX_SIZE = 500

# ── Add the IDs of bots whose V2 messages you want auto-converted ─────────────
# Leave empty [] to convert ALL bots' V2 messages
AUTO_CONVERT_BOT_IDS = [
    # 000000000000000000,  # Pokétwo
    # add more bot IDs here
]

_converted: OrderedDict[int, bool] = OrderedDict()
_CONVERTED_MAX = 1000

# MESSAGE_UPDATE payloads often omit `author`, so we keep a map of
# message_id → author info gathered from MESSAGE_CREATE.
_author_cache: OrderedDict[int, dict] = OrderedDict()
_AUTHOR_CACHE_MAX = 2000


def _store_author(message_id: int, author: dict):
    if message_id not in _author_cache:
        if len(_author_cache) >= _AUTHOR_CACHE_MAX:
            _author_cache.popitem(last=False)
    _author_cache[message_id] = author


def _get_author(message_id: int, payload_author: dict) -> dict:
    """Return author from payload if present, else fall back to cache."""
    if payload_author:
        return payload_author
    return _author_cache.get(message_id, {})


def _mark_converted(message_id: int):
    if message_id not in _converted:
        if len(_converted) >= _CONVERTED_MAX:
            _converted.popitem(last=False)
        _converted[message_id] = True


def _already_converted(message_id: int) -> bool:
    return message_id in _converted


# ── Component walking ─────────────────────────────────────────────────────────

def _walk(components: list, result: dict):
    for comp in components:
        ctype = comp.get("type")

        if ctype == 17:        # Container
            if result["color"] is None and comp.get("accent_color") is not None:
                result["color"] = comp["accent_color"]
            _walk(comp.get("components", []), result)

        elif ctype == 1:       # ActionRow
            _walk(comp.get("components", []), result)

        elif ctype == 25:      # Section — children in "components", accessory separate
            _walk(comp.get("components", []), result)
            accessory = comp.get("accessory")
            if accessory:
                _walk([accessory], result)

        elif ctype == 10:      # TextDisplay
            content = comp.get("content", "").strip()
            if content:
                result["text"].append(content)

        elif ctype == 14:      # Separator
            pass

        elif ctype == 9:       # Thumbnail
            url = (comp.get("media") or {}).get("url", "")
            desc = comp.get("description", "")
            if url and result["thumbnail"] is None:
                result["thumbnail"] = (url, desc)

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
            label = comp.get("label", "")
            url   = comp.get("url")
            emoji = (comp.get("emoji") or {}).get("name", "")
            display = f"{emoji} {label}".strip()
            if display:
                result["buttons"].append((display, url))

        elif ctype == 3:       # StringSelect
            placeholder = comp.get("placeholder", "Select an option")
            options     = [o.get("label", "") for o in comp.get("options", [])]
            result["selects"].append((placeholder, options))

        else:
            # Unknown type — recurse into children if any
            _walk(comp.get("components", []), result)


def _parse(raw_components: list) -> dict:
    result = {"text": [], "buttons": [], "selects": [],
              "images": [], "thumbnail": None, "color": None}
    _walk(raw_components, result)
    return result


def _build_embeds(message: discord.Message, raw_components: list) -> list[discord.Embed]:
    data = _parse(raw_components)

    if message.content and message.content.strip():
        data["text"].insert(0, message.content.strip())

    for embed in message.embeds:
        if embed.description:
            data["text"].append(embed.description)
        for field in embed.fields:
            data["text"].append(f"**{field.name}**\n{field.value}")
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
            data["text"].append(f"📎 [{att.filename}]({att.url})")

    color = discord.Color(data["color"]) if data["color"] else discord.Color.blurple()

    desc_parts = list(data["text"])
    if data["buttons"]:
        desc_parts.append("**Buttons:** " + " · ".join(
            f"[{l}]({u})" if u else f"`{l}`" for l, u in data["buttons"]
        ))
    if data["selects"]:
        for ph, opts in data["selects"]:
            desc_parts.append(f"**{ph}:** " + (", ".join(f"`{o}`" for o in opts) or "*no options*"))

    description = "\n\n".join(desc_parts) if desc_parts else "*No readable content.*"
    if len(description) > 4090:
        description = description[:4087] + "…"

    main = discord.Embed(description=description, color=color, timestamp=message.created_at)
    main.set_footer(text=f"#{message.channel.name}  •  Components V2 → Embed")

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

    return embeds


async def _get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.name == "V2 Converter":
            return wh
    return await channel.create_webhook(name="V2 Converter")


async def _send_via_webhook(message: discord.Message, embeds: list[discord.Embed]):
    channel = message.channel

    if isinstance(channel, discord.Thread):
        wh = await _get_or_create_webhook(channel.parent)
        await wh.send(
            embeds=embeds,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
            thread=channel,
        )
    elif isinstance(channel, discord.TextChannel):
        wh = await _get_or_create_webhook(channel)
        await wh.send(
            embeds=embeds,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
        )
    else:
        await channel.send(embeds=embeds)


def _is_v2_message(payload: dict, components: list) -> bool:
    """
    Detect a Components V2 message via either:
      - A top-level Container (type 17) in components, OR
      - Discord's IS_COMPONENTS_V2 flag (bit 15 = 32768)
    The flag alone (without components) is not enough — we need something to convert.
    """
    has_container = any(c.get("type") == 17 for c in components)
    flag_v2       = bool(payload.get("flags", 0) & (1 << 15))
    return (has_container or flag_v2) and bool(components)


class ConverterCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: OrderedDict[int, list] = OrderedDict()

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

        # Cache author from CREATE so UPDATE (which omits it) can look it up
        raw_author = payload.get("author", {})
        if raw_author:
            _store_author(message_id, raw_author)

        author    = _get_author(message_id, raw_author)
        author_id = int(author.get("id", 0))
        is_bot    = author.get("bot", False)

        if not _is_v2_message(payload, components):
            return

        if components:
            self._store(message_id, components)

        if not is_bot:
            return

        if AUTO_CONVERT_BOT_IDS and author_id not in AUTO_CONVERT_BOT_IDS:
            return

        if _already_converted(message_id):
            return

        channel_id = int(payload.get("channel_id", 0))
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        try:
            message = await channel.fetch_message(message_id)
        except Exception:
            return

        embeds = _build_embeds(message, components)
        if not embeds or embeds[0].description == "*No readable content.*":
            return

        _mark_converted(message_id)
        await _send_via_webhook(message, embeds)

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

    async def _do_convert(self, target: discord.Message) -> tuple[str, list[discord.Embed]]:
        raw = self._get(target.id)
        note = "-# ✅ Source: Components V2 (live cache)" if raw else \
               "-# ⚠️ Not in cache — message may have been sent before bot started."
        embeds = _build_embeds(target, raw or [])
        intro  = (
            f"📬 **Converted** from {target.author.mention} — "
            f"[Jump to original]({target.jump_url})\n{note}"
        )
        return intro, embeds

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
            intro, embeds = await self._do_convert(target)
            await ctx.reply(content=intro, embeds=embeds)
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
            intro, embeds = await self._do_convert(target)
            await interaction.followup.send(content=intro, embeds=embeds)
        except Exception as e:
            await interaction.followup.send(f"❌ Error:\n```\n{type(e).__name__}: {e}\n```")


async def setup(bot: commands.Bot):
    await bot.add_cog(ConverterCog(bot))
