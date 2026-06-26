"""
cogs/poketwo_prank_cog.py
─────────────────────────
Whenever anyone pings Pokétwo (<@716390085896962058>), the bot sends
a fake "Account Suspended" embed via webhook in that same channel,
impersonating Pokétwo.
"""

import re
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

POKETWO_ID   = 716390085896962058
POKETWO_NAME = "Pokétwo"
POKETWO_AVA  = "https://cdn.discordapp.com/avatars/716390085896962058/3031fa9e2fabde1652a57ab33f4d7f37.webp?size=128"

_MENTION_RE = re.compile(rf"<@!?{POKETWO_ID}>")


async def _get_or_create_webhook(channel: discord.TextChannel) -> Optional[discord.Webhook]:
    try:
        for hook in await channel.webhooks():
            if hook.name == "PokétwoPrank":
                return hook
        return await channel.create_webhook(name="PokétwoPrank")
    except discord.Forbidden:
        return None


class PokétwoPrankCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not _MENTION_RE.search(message.content or ""):
            return

        embed = discord.Embed(
            title="Account Suspended",
            description=(
                "Your account was found to be in violation of the "
                "[Pokétwo Terms of Service](https://poketwo.net/terms) "
                "and has been blacklisted from Pokétwo."
            ),
            colour=discord.Colour(0xE74C3C),
        )
        embed.add_field(name="Reason", value="gambling/crosstrading", inline=False)
        embed.add_field(
            name="Appeals",
            value=(
                "If, after reading and understanding the reason provided above, "
                "you believe your account was suspended in error, and that you did "
                "not violate the Terms of Service, you may submit a "
                "[Bot Suspension Appeal](https://forms.poketwo.net/a/suspension-appeal) "
                "to request a re-review of your case."
            ),
            inline=False,
        )

        webhook = await _get_or_create_webhook(message.channel)
        if webhook is None:
            await message.channel.send(embed=embed)
            return

        async with aiohttp.ClientSession() as session:
            wh = discord.Webhook.from_url(webhook.url, session=session)
            await wh.send(embed=embed, username=POKETWO_NAME, avatar_url=POKETWO_AVA)


async def setup(bot: commands.Bot):
    await bot.add_cog(PokétwoPrankCog(bot))
