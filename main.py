import discord
from discord.ext import commands
import os
import asyncio
import db
import pokedata

TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")

intents = discord.Intents.default()
intents.message_content = True

# enable_debug_events=True is REQUIRED for on_socket_raw_receive to fire (converter cog)
bot = commands.Bot(
    command_prefix=["a!", "A!", "!"],
    help_command=None,
    intents=intents,
    enable_debug_events=True,
)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Servers: {len(bot.guilds)}")


async def main():
    pokedata.load()
    await db.ensure_indexes()   # create query indexes (idempotent)
    async with bot:
        await bot.load_extension("converter_cog")
        await bot.load_extension("cogs.help_cog")
        await bot.load_extension("cogs.tracker_cog")
        await bot.load_extension("cogs.leaderboard_cog")
        await bot.load_extension("cogs.calculator_cog")
        await bot.load_extension("cogs.category_cog")
        await bot.start(TOKEN)


asyncio.run(main())
