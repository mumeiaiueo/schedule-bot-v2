import os
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from supabase import create_client

# ========= env =========
TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が未設定です")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_KEY が未設定です")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
JST = timezone(timedelta(hours=9))

# ========= discord =========
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---- 状態（作成中の一時保存）----
draft = {}  # key: (guild_id, user_id) -> dict

def dkey(interaction: discord.Interaction):
    return (str(interaction.guild_id), str(interaction.user.id))

async def db_to_thread(fn):
    return await asyncio.to_thread(fn)

# ========= DB helpers =========
def upsert_panel(row: dict):
    return sb.table("panels").upsert(row, on_conflict="guild_id,day_key").execute()

# ========= UI helpers =========
def hour_options():
    return [discord.SelectOption(label=f"{h:02d}", value=f"{h:02d}") for h in range(24)]  # 24

def minute_options(step=5):
    return [discord.SelectOption(label=f"{m:02d}", value=f"{m:02d}") for m in range(0, 60, step)]  # 12

def interval_options():
    return [
        discord.SelectOption(label="20分", value="20"),
        discord.SelectOption(label="25分", value="25"),
        discord.SelectOption(label="30分", value="30"),
    ]

def hm_from_state(st: dict, prefix: str):
    h = st.get(f"{prefix}_h")
    m = st.get(f"{prefix}_m")
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"

def build_setup_embed(st: dict):
    e = discord.Embed(title="募集パネル作成", color=0x5865F2)

    day = st.get("day_key", "today")
    e.add_field(name="日付", value=("今日" if day == "today" else "明日"), inline=True)

    start = hm_from_state(st, "start")
    end = hm_from_state(st, "end")
    e.add_field(name="開始", value=(start or "未選択"), inline=True)
    e.add_field(name="終了", value=(end or "未選択"), inline=True)

    interval = st.get("interval_minutes")
    e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)

    title = st.get("title") or "無題"
    e.add_field(name="タイトル", value=title, inline=False)

    notify = st.get("notify_channel_id")
    e.add_field(name="通知チャンネル", value=(f"<#{notify}>" if notify else "このチャンネル"), inline=False)

    everyone = bool(st.get("mention_everyone", False))
    e.add_field(name="@everyone", value=("ON" if everyone else "OFF"), inline=True)

    e.set_footer(text="全部選んだら「作成」")
    return e

# ========= Modal =========
class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False)

    def __init__(self, st: dict):
        super().__init__(timeout=300)
        self.st = st

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        await interaction.response.send_message("✅ タイトルを保存しました", ephemeral=True)

# ========= View =========
class SetupView(discord.ui.View):
    def __init__(self, st: dict):
        super().__init__(timeout=None)
        self.st = st

        # Row0: day buttons
        self.add_item(discord.ui.Button(label="今日", style=discord.ButtonStyle.primary, custom_id="setup:day:today", row=0))
        self.add_item(discord.ui.Button(label="明日", style=discord.ButtonStyle.secondary, custom_id="setup:day:tomorrow", row=0))

        # Row1: start hour/min
        self.add_item(discord.ui.Select(custom_id="setup:start_h", placeholder="開始(時)", options=hour_options(), row=1))
        self.add_item(discord.ui.Select(custom_id="setup:start_m", placeholder="開始(分)", options=minute_options(5), row=2))

        # Row3: end hour/min
        self.add_item(discord.ui.Select(custom_id="setup:end_h", placeholder="終了(時)", options=hour_options(), row=3))
        self.add_item(discord.ui.Select(custom_id="setup:end_m", placeholder="終了(分)", options=minute_options(5), row=4))

        # Row0 (残り枠): interval + title + everyone
        self.add_item(discord.ui.Select(custom_id="setup:interval", placeholder="間隔（20/25/30）", options=interval_options(), row=0))
        self.add_item(discord.ui.Button(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=1))
        self.add_item(discord.ui.Button(label="@everyone ON/OFF", style=discord.ButtonStyle.danger, custom_id="setup:everyone", row=1))

        # Row2: notify channel
        cs = discord.ui.ChannelSelect(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（未選択=このチャンネル）",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=2
        )
        self.add_item(cs)

        # Row3: create
        self.add_item(discord.ui.Button(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=3))

# ========= Component handler（ここが超重要） =========
@client.event
async def on_interaction(interaction: discord.Interaction):
    try:
        # ① スラッシュコマンド（/setup /generate /reset など）は必ず tree に渡す
        if interaction.type == discord.InteractionType.application_command:
            await tree._call(interaction)
            return

        # ② ボタン/セレクト/チャンネルセレクトなど（component）
        if interaction.type == discord.InteractionType.component:
            data = interaction.data or {}
            cid = data.get("custom_id") or ""

            # setup系だけ自前処理したいならここで処理（※あなたのdraft反映用）
            if cid.startswith("setup:"):
                key = dkey(interaction)
                if key in draft:
                    st = draft[key]
                    values = data.get("values") or []

                    # Select反映（あなたのcustom_idに合わせて）
                    if cid == "setup:start_h" and values:
                        st["start_h"] = int(values[0])
                    elif cid == "setup:start_m" and values:
                        st["start_m"] = int(values[0])
                    elif cid == "setup:end_h" and values:
                        st["end_h"] = int(values[0])
                    elif cid == "setup:end_m" and values:
                        st["end_m"] = int(values[0])
                    elif cid == "setup:interval_select" and values:
                        st["interval"] = int(values[0])

                # 重要：componentは必ずACK（edit_messageかdeferのどちらか）
                if not interaction.response.is_done():
                    # ここは「UI更新してる」なら edit_message が一番安全
                    await interaction.response.defer()  # ← ひとまずACKだけ（ボタン側callbackが動くならここ不要）
                return

        # ③ それ以外は何もしない（discord.py標準の挙動に任せる）
        return

    except Exception as e:
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ on_interaction error: {e}", ephemeral=True)
        except Exception:
            pass

# ========= command =========
@tree.command(name="setup", description="募集パネルを作る（設定画面を出す）")
async def setup(interaction: discord.Interaction):
    key = dkey(interaction)
    draft[key] = {
        "day_key": "today",
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
    }
    st = draft[key]
    await interaction.response.send_message("設定して「作成」👇", embed=build_setup_embed(st), view=SetupView(st), ephemeral=False)

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

async def main():
    await client.start(TOKEN)

asyncio.run(main())