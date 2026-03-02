import os
import asyncio
from datetime import datetime
import discord
from discord import app_commands
from supabase import create_client

TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---- 状態（作成中の一時保存）----
draft = {}  # key: (guild_id, user_id) -> dict

def dkey(interaction: discord.Interaction):
    return (str(interaction.guild_id), str(interaction.user.id))

async def db_to_thread(fn):
    return await asyncio.to_thread(fn)

def upsert_panel(row: dict):
    return sb.table("panels").upsert(row, on_conflict="guild_id,day_key").execute()

# ---- UI ----
class SetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title")
    async def title_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TitleModal())

    @discord.ui.button(label="間隔 25分", style=discord.ButtonStyle.secondary, custom_id="setup:interval")
    async def interval_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = draft.setdefault(dkey(interaction), {})
        st["interval_minutes"] = 25
        await interaction.response.send_message("✅ 間隔を 25分 にしました", ephemeral=True)

    @discord.ui.button(label="@everyone ON/OFF", style=discord.ButtonStyle.danger, custom_id="setup:everyone")
    async def everyone_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = draft.setdefault(dkey(interaction), {})
        st["mention_everyone"] = not st.get("mention_everyone", False)
        await interaction.response.send_message(f"✅ @everyone = {st['mention_everyone']}", ephemeral=True)

    @discord.ui.button(label="通知チャンネル=ここ", style=discord.ButtonStyle.secondary, custom_id="setup:notify_here")
    async def notify_here_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = draft.setdefault(dkey(interaction), {})
        st["notify_channel_id"] = str(interaction.channel_id)
        await interaction.response.send_message("✅ 通知チャンネルをこのチャンネルにしました", ephemeral=True)

    @discord.ui.button(label="作成", style=discord.ButtonStyle.green, custom_id="setup:create")
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = draft.get(dkey(interaction), {})
        title = st.get("title", "無題")
        interval = int(st.get("interval_minutes", 30))
        mention_everyone = bool(st.get("mention_everyone", False))
        notify_channel_id = st.get("notify_channel_id")  # None可

        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),
            "day_key": "today",
            "title": title,
            "interval_minutes": interval,
            "notify_channel_id": notify_channel_id,
            "mention_everyone": mention_everyone,
            "created_by": str(interaction.user.id),
            "created_at": datetime.utcnow().isoformat(),
        }

        await interaction.response.defer(ephemeral=True)
        try:
            await db_to_thread(lambda: upsert_panel(row))
        except Exception as e:
            await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
            return

        await interaction.followup.send("✅ 保存できた！次は枠ボタン生成に進めるよ", ephemeral=True)

class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        st = draft.setdefault(dkey(interaction), {})
        st["title"] = str(self.name.value)
        await interaction.response.send_message("✅ タイトルを保存しました", ephemeral=True)

@tree.command(name="setup", description="作成パネルを表示")
async def setup(interaction: discord.Interaction):
    draft[dkey(interaction)] = {
        "interval_minutes": 30,
        "mention_everyone": False,
        "title": "無題",
        "notify_channel_id": None,
    }
    await interaction.response.send_message(
        "設定して「作成」👇",
        view=SetupView(),
        ephemeral=False
    )

@client.event
async def on_ready():
    client.add_view(SetupView())
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

async def main():
    await asyncio.sleep(10)  # 429避け
    await client.start(TOKEN)

asyncio.run(main())