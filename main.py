import os
import discord

intents = discord.Intents.default()
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print("✅ Logged in as", bot.user)

bot.run(os.getenv("DISCORD_TOKEN"))