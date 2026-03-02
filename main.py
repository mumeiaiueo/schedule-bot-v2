import os
import asyncio
import discord

TOKEN = os.getenv("DISCORD_TOKEN")  # Renderの環境変数名に合わせて

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user} (id={client.user.id})")

async def main():
    await asyncio.sleep(10)  # ←これが429予防の保険
    await client.start(TOKEN)

asyncio.run(main())