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
    # panels に (guild_id, day_key) のユニーク制約がある前提
    return sb.table("panels").upsert(row, on_conflict="guild_id,day_key").execute()

def get_panel(guild_id: str):
    return sb.table("panels").select("*").eq("guild_id", guild_id).eq("day_key", "today").limit(1).execute()

def insert_slots(rows: list[dict]):
    return sb.table("slots").insert(rows).execute()

def update_panel_message_id(panel_id: int, message_id: str):
    return sb.table("panels").update({"panel_message_id": message_id}).eq("id", panel_id).execute()

def slot_label(dt: datetime):
    return dt.astimezone(JST).strftime("%H:%M")

# ========= UI: setup =========
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

        await interaction.followup.send("✅ 保存できた！次は /generate で枠ボタン生成してね", ephemeral=True)

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
        "設定して「作成」👇（作成後は /generate）",
        view=SetupView(),
        ephemeral=False
    )

# ========= UI: slots =========
class SlotsView(discord.ui.View):
    def __init__(self, slot_rows: list[dict]):
        super().__init__(timeout=None)
        # ボタン最大25個。まずは20個に制限
        for r in slot_rows[:20]:
            sid = r["id"]
            st = datetime.fromisoformat(str(r["start_at"]).replace("Z", "+00:00"))
            label = slot_label(st)
            self.add_item(SlotButton(label=label, slot_id=sid))

class SlotButton(discord.ui.Button):
    def __init__(self, label: str, slot_id: int):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"slot:{slot_id}")
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        # 予約済みチェック
        def work_get():
            return sb.table("slots").select("reserved_by").eq("id", self.slot_id).limit(1).execute()
        res = await db_to_thread(work_get)

        if res.data and res.data[0].get("reserved_by"):
            await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
            return

        # 予約
        def work_set():
            return sb.table("slots").update({"reserved_by": user_id}).eq("id", self.slot_id).execute()
        await db_to_thread(work_set)

        await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

@tree.command(name="generate", description="今日の枠ボタンを生成して投稿")
async def generate(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id)

    # 1) panels 取得
    try:
        pres = await db_to_thread(lambda: get_panel(guild_id))
    except Exception as e:
        await interaction.followup.send(f"❌ panels 取得失敗: {e}", ephemeral=True)
        return

    if not pres.data:
        await interaction.followup.send("❌ 先に /setup → 作成 をしてね", ephemeral=True)
        return

    panel = pres.data[0]
    panel_id = panel["id"]
    title = panel.get("title", "無題")
    interval = int(panel.get("interval_minutes", 30))
    notify_channel_id = panel.get("notify_channel_id") or str(interaction.channel_id)

    # 2) 今から2時間分生成（まず固定で動かす）
    now = datetime.now(JST)
    minute = (now.minute // interval) * interval
    start = now.replace(minute=minute, second=0, microsecond=0)
    end = start + timedelta(hours=2)

    # 3) slots insert
    slot_rows = []
    cur = start
    while cur < end:
        slot_rows.append({
            "panel_id": panel_id,
            "start_at": cur.astimezone(timezone.utc).isoformat(),
            "end_at": (cur + timedelta(minutes=interval)).astimezone(timezone.utc).isoformat(),
            "reserved_by": None
        })
        cur += timedelta(minutes=interval)

    try:
        ins = await db_to_thread(lambda: insert_slots(slot_rows))
    except Exception as e:
        await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)
        return

    created = ins.data or []
    if not created:
        await interaction.followup.send("❌ slots が作れなかった（slots の列が足りない可能性）", ephemeral=True)
        return

    # 4) パネル投稿
    ch = interaction.guild.get_channel(int(notify_channel_id)) or interaction.channel
    msg = await ch.send(f"📅 **{title}**\n下のボタンで予約してね👇", view=SlotsView(created))

    # panels に message_id 保存（失敗してもOK）
    try:
        await db_to_thread(lambda: update_panel_message_id(panel_id, str(msg.id)))
    except Exception:
        pass

    await interaction.followup.send("✅ 枠ボタンを生成して投稿した！", ephemeral=True)

# ========= lifecycle =========
@client.event
async def on_ready():
    # setup のボタンは常時生かす（/setup パネルが過去に残ってても押せる）
    client.add_view(SetupView())
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

async def main():
    await asyncio.sleep(10)  # 429避け
    await client.start(TOKEN)

asyncio.run(main())