import os
import asyncio
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# --- ボタンのUI ---
class CreatePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 常駐

    @discord.ui.button(label="作成", style=discord.ButtonStyle.green, custom_id="setup:create")
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # まずは確実に反応する確認（ephemeralでOK）
        await interaction.response.send_message("✅ 作成ボタン押された！", ephemeral=True)

@tree.command(name="setup", description="作成パネルを表示（テスト）")
async def setup(interaction: discord.Interaction):
    await interaction.response.send_message(
        "テスト用パネル👇（作成を押して）",
        view=CreatePanelView(),
        ephemeral=False
    )

@client.event
async def on_ready():
    # 再起動してもボタンが生きるように登録
    client.add_view(CreatePanelView())
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

async def main():
    await asyncio.sleep(10)  # レート制限予防
    await client.start(TOKEN)

asyncio.run(main())