import os
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv

from db import init_supabase, db_to_thread
from views.setup_view import build_setup_view, build_setup_embed
from views.setup_view import TitleModal
from views.slots_view import SlotsView

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

# ===== 起動時にSupabase初期化 =====
sb = init_supabase()

# ===== discord =====
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ====== ウィザード状態（ユーザーごと） ======
wizard_state: dict[int, dict] = {}

def ensure_state(user_id: int) -> dict:
    st = wizard_state.get(user_id)
    if not isinstance(st, dict):
        st = {
            "step": 1,
            "day": "today",  # today / tomorrow
            "start_hour": None,
            "start_min": None,
            "end_hour": None,
            "end_min": None,
            "interval": None,  # "20"/"25"/"30"
            "title": "",
            "mention_everyone": False,
            "notify_channel_id": None,  # int
        }
        wizard_state[user_id] = st
    return st

def hm_to_minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)

def get_hm(st: dict, key_h: str, key_m: str):
    if st.get(key_h) is None or st.get(key_m) is None:
        return None
    return f"{int(st[key_h]):02d}:{int(st[key_m]):02d}"

def day_key_to_date(day_key: str) -> datetime.date:
    now = datetime.now(JST)
    if day_key == "tomorrow":
        return (now + timedelta(days=1)).date()
    return now.date()

def build_range_jst(day_date, sh, sm, eh, em):
    start = datetime(day_date.year, day_date.month, day_date.day, sh, sm, tzinfo=JST)

    # 24:00対応（終了が 24:00 のとき）
    if eh == 24 and em == 0:
        end = datetime(day_date.year, day_date.month, day_date.day, 0, 0, tzinfo=JST) + timedelta(days=1)
        return start, end

    end = datetime(day_date.year, day_date.month, day_date.day, eh, em, tzinfo=JST)

    # 日跨ぎ（例 23:00 -> 01:00）
    if end <= start:
        end = end + timedelta(days=1)

    return start, end

def slot_time_label(dt_utc: datetime) -> str:
    return dt_utc.astimezone(JST).strftime("%H:%M")

# ===== DB helpers =====
def upsert_panel(row: dict):
    return sb.table("panels").upsert(row, on_conflict="guild_id,day_key").execute()

def get_panel(guild_id: str, day_key: str):
    return (
        sb.table("panels")
        .select("*")
        .eq("guild_id", guild_id)
        .eq("day_key", day_key)
        .limit(1)
        .execute()
    )

def delete_slots(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()

def insert_slots(rows: list[dict]):
    return sb.table("slots").insert(rows).execute()

def update_panel_message_id(panel_id: int, message_id: str):
    return sb.table("panels").update({"panel_message_id": message_id}).eq("id", panel_id).execute()

def delete_panel(guild_id: str, day_key: str):
    return sb.table("panels").delete().eq("guild_id", guild_id).eq("day_key", day_key).execute()

# ===== /setup =====
@tree.command(name="setup", description="募集パネル作成ウィザードを開く")
async def setup_cmd(interaction: discord.Interaction):
    st = ensure_state(interaction.user.id)
    st["step"] = 1  # 最初に戻す

    await interaction.response.send_message(
        embed=build_setup_embed(st),
        view=build_setup_view(st),
        ephemeral=False
    )

# ===== /generate =====
@tree.command(name="generate", description="設定済みの内容で枠ボタンを生成して投稿")
async def generate_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id)
    # day_key は今は今日固定（必要なら tomorrow も対応できる）
    day_key = "today"

    # panel 取得
    pres = await db_to_thread(lambda: get_panel(guild_id, day_key))
    if not pres.data:
        await interaction.followup.send("❌ 先に /setup → 保存 してね", ephemeral=True)
        return

    panel = pres.data[0]
    panel_id = int(panel["id"])

    title = panel.get("title") or "無題"
    interval = int(panel.get("interval_minutes") or 30)
    notify_channel_id = panel.get("notify_channel_id") or str(interaction.channel_id)
    mention_everyone = bool(panel.get("mention_everyone", False))

    # 時刻
    start_hm = panel.get("start_hm")
    end_hm = panel.get("end_hm")
    if not start_hm or not end_hm:
        await interaction.followup.send("❌ 開始/終了が保存されてない。/setup からやり直してね", ephemeral=True)
        return

    sh, sm = [int(x) for x in str(start_hm).split(":")]
    eh, em = [int(x) for x in str(end_hm).split(":")]

    day_date = day_key_to_date(day_key)
    start_jst, end_jst = build_range_jst(day_date, sh, sm, eh, em)

    # slots 作り直し（重複防止：毎回削除して作成）
    await db_to_thread(lambda: delete_slots(panel_id))

    rows = []
    cur = start_jst
    while cur < end_jst:
        st_utc = cur.astimezone(UTC)
        en_utc = (cur + timedelta(minutes=interval)).astimezone(UTC)
        rows.append({
            "panel_id": panel_id,
            "start_at": st_utc.isoformat(),
            "end_at": en_utc.isoformat(),
            "reserved_by": None,
            "slot_time": slot_time_label(st_utc),  # NOT NULL対策
        })
        cur += timedelta(minutes=interval)

    ins = await db_to_thread(lambda: insert_slots(rows))
    created = ins.data or []
    if not created:
        await interaction.followup.send("❌ slots が作れなかった（slotsテーブル列を確認）", ephemeral=True)
        return

    # 投稿チャンネル
    ch = interaction.guild.get_channel(int(notify_channel_id)) or interaction.channel

    header = f"📅 **{title}**\n下のボタンで予約してね👇"
    if mention_everyone:
        header = "@everyone\n" + header

    msg = await ch.send(header, view=SlotsView(sb, db_to_thread, panel_id, created))

    # message_id保存
    try:
        await db_to_thread(lambda: update_panel_message_id(panel_id, str(msg.id)))
    except Exception:
        pass

    # @everyone は「1回だけ」仕様：使ったらOFFに戻す
    if mention_everyone:
        try:
            await db_to_thread(lambda: upsert_panel({
                "guild_id": str(interaction.guild_id),
                "day_key": day_key,
                "mention_everyone": False,
            }))
        except Exception:
            pass

    await interaction.followup.send("✅ 枠ボタンを生成して投稿した！", ephemeral=True)

# ===== /reset =====
@tree.command(name="reset", description="今日の募集を削除（パネル＆枠）")
async def reset_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id)
    day_key = "today"

    pres = await db_to_thread(lambda: get_panel(guild_id, day_key))
    if pres.data:
        panel = pres.data[0]
        panel_id = int(panel["id"])

        # slots削除
        await db_to_thread(lambda: delete_slots(panel_id))

        # 投稿メッセ削除（できれば）
        mid = panel.get("panel_message_id")
        ch_id = panel.get("notify_channel_id") or panel.get("channel_id")
        if mid and ch_id:
            try:
                ch = interaction.guild.get_channel(int(ch_id))
                if ch:
                    m = await ch.fetch_message(int(mid))
                    await m.delete()
            except Exception:
                pass

    # panel削除
    await db_to_thread(lambda: delete_panel(guild_id, day_key))

    await interaction.followup.send("✅ 今日の募集を削除したよ（パネル＆枠）", ephemeral=True)

# ===== interaction handler（ここが肝：応答なし対策の中心） =====
@client.event
async def on_interaction(interaction: discord.Interaction):
    # スラッシュはdiscord.pyに任せる
    if interaction.type != discord.InteractionType.component:
        return

    data = interaction.data or {}
    cid = data.get("custom_id") or ""
    if not cid.startswith("setup:"):
        return

    st = ensure_state(interaction.user.id)

    # ---- ボタン ----
    if cid.startswith("setup:day:"):
        st["day"] = cid.split(":")[-1]

    elif cid == "setup:step:2":
        st["step"] = 2

    elif cid == "setup:step:1":
        st["step"] = 1

    elif cid == "setup:everyone:toggle":
        st["mention_everyone"] = not bool(st.get("mention_everyone", False))

    elif cid == "setup:title:open":
        # モーダルは defer してると出せないので、そのまま送る
        await interaction.response.send_modal(TitleModal(st))
        return

    elif cid == "setup:save":
        # バリデーション
        start = get_hm(st, "start_hour", "start_min")
        end = get_hm(st, "end_hour", "end_min")
        interval = st.get("interval")

        if not start or not end:
            await interaction.response.send_message("❌ 開始/終了を選んでね", ephemeral=True)
            return
        if not interval:
            await interaction.response.send_message("❌ 間隔を選んでね", ephemeral=True)
            return

        # 時刻整合（同日で end<=start でも日跨ぎとしてOKにするので、ここは弾かない）
        notify_ch = st.get("notify_channel_id") or int(interaction.channel_id)

        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),
            "day_key": st.get("day", "today"),
            "title": (st.get("title") or "").strip(),
            "interval_minutes": int(interval),
            "notify_channel_id": str(notify_ch),
            "mention_everyone": bool(st.get("mention_everyone", False)),
            "start_hm": start,
            "end_hm": end,
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
        return

    # ---- Selects ----
    values = data.get("values") or []
    if values:
        val = values[0]
        if cid == "setup:start_hour":
            st["start_hour"] = int(val)
        elif cid == "setup:start_min":
            st["start_min"] = int(val)
        elif cid == "setup:end_hour":
            st["end_hour"] = int(val)
        elif cid == "setup:end_min":
            st["end_min"] = int(val)
        elif cid == "setup:interval":
            st["interval"] = int(val)
        elif cid == "setup:notify_channel":
            # channel select はID文字列が入る
            st["notify_channel_id"] = int(val)

    # 画面更新（これが「応答なし」を減らす）
    embed = build_setup_embed(st)
    view = build_setup_view(st)

    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.message.edit(embed=embed, view=view)
    except Exception:
        pass

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN 未設定")
    # 429避け
    await asyncio.sleep(3)
    await client.start(TOKEN.strip())

asyncio.run(main())