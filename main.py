import discord
from discord.ext import commands
import os
import asyncio

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

intents = discord.Intents.default()
intents.message_content = True

# enable_debug_events=True is REQUIRED for on_socket_raw_receive to fire
bot = commands.Bot(command_prefix="!", intents=intents, enable_debug_events=True)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Servers: {len(bot.guilds)}")

async def main():
    async with bot:
        await bot.load_extension("converter_cog")
        await bot.start(TOKEN)

asyncio.run(main())
