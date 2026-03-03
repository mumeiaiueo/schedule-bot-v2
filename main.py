import os
import asyncio
from datetime import datetime, timedelta, timezone, date

import discord
from discord import app_commands
from supabase import create_client

# =====================
# ENV
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が未設定です")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_KEY が未設定です")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

JST = timezone(timedelta(hours=9))

# =====================
# Discord
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# draft: setup中の状態（ユーザー×サーバー）
draft: dict[tuple[str, str], dict] = {}


def dkey(interaction: discord.Interaction) -> tuple[str, str]:
    return (str(interaction.guild_id), str(interaction.user.id))


async def db_to_thread(fn):
    return await asyncio.to_thread(fn)


# =====================
# DB helpers
# =====================
def upsert_panel(row: dict):
    # panels に (guild_id, day_key) UNIQUE がある前提
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


def update_panel_message_id(panel_id: int, message_id: str):
    return sb.table("panels").update({"panel_message_id": message_id}).eq("id", panel_id).execute()


def delete_slots(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()


def insert_slots(rows: list[dict]):
    return sb.table("slots").insert(rows).execute()


def get_slots(panel_id: int):
    return sb.table("slots").select("*").eq("panel_id", panel_id).order("start_at").execute()


def get_slot(slot_id: int):
    return sb.table("slots").select("*").eq("id", slot_id).limit(1).execute()


def reserve_slot(slot_id: int, user_id: str, user_name: str):
    return (
        sb.table("slots")
        .update(
            {
                "reserved_by": user_id,
                "reserver_user_id": int(user_id),
                "reserver_name": user_name,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", slot_id)
        .execute()
    )


def cancel_slot(slot_id: int):
    return (
        sb.table("slots")
        .update(
            {
                "reserved_by": None,
                "reserver_user_id": None,
                "reserver_name": None,
                "reserved_at": None,
            }
        )
        .eq("id", slot_id)
        .execute()
    )


# =====================
# UI helpers
# =====================
def hour_options():
    return [discord.SelectOption(label=f"{h:02d}", value=f"{h:02d}") for h in range(24)]


def minute_options(step=5):
    return [discord.SelectOption(label=f"{m:02d}", value=f"{m:02d}") for m in range(0, 60, step)]


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


def parse_hm_to_datetime(day_base: date, hm: str) -> datetime:
    # hm "18:25"
    hh, mm = hm.split(":")
    return datetime(day_base.year, day_base.month, day_base.day, int(hh), int(mm), tzinfo=JST)


def slot_label(dt_utc_iso: str) -> str:
    dt = datetime.fromisoformat(dt_utc_iso.replace("Z", "+00:00"))
    return dt.astimezone(JST).strftime("%H:%M")


def build_setup_embed(st: dict) -> discord.Embed:
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)

    step = int(st.get("step", 1))
    day_key = st.get("day_key", "today")

    e.add_field(name="Step", value=str(step), inline=False)
    e.add_field(name="日付", value=("今日" if day_key == "today" else "明日"), inline=True)

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

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e


def build_panel_embed(panel: dict, slots: list[dict]) -> discord.Embed:
    day_key = panel.get("day_key", "today")
    title = panel.get("title") or "募集パネル"
    interval = panel.get("interval_minutes") or 30

    e = discord.Embed(title=title, color=0x2B2D31)
    e.add_field(name="日付", value=("今日(JST)" if day_key == "today" else "明日(JST)"), inline=True)
    e.add_field(name="interval", value=f"{interval}min", inline=True)

    lines = []
    for s in slots:
        if s.get("is_break"):
            lines.append(f"⚪ {s.get('slot_time','')}")
            continue

        reserved_by = s.get("reserved_by")
        t = s.get("slot_time") or slot_label(str(s["start_at"]))
        if reserved_by:
            name = s.get("reserver_name")
            mention = f"<@{reserved_by}>"
            lines.append(f"🔴 {t} {mention if not name else mention}")
        else:
            lines.append(f"🟢 {t}")

    if not lines:
        lines = ["(枠がありません)"]

    e.add_field(name="枠", value="\n".join(lines[:25]), inline=False)
    e.add_field(
        name="凡例",
        value="🟢 空き / 🔴 予約済み（本人は押すとキャンセル） / ⚪ 休憩（予約不可）",
        inline=False,
    )
    return e


# =====================
# Modal
# =====================
class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False)

    def __init__(self, st: dict, message: discord.Message):
        super().__init__(timeout=300)
        self.st = st
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        # 反映（embed更新）
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupView(self.st, self.message))


# =====================
# Setup View（Step1/Step2）
# =====================
class SetupView(discord.ui.View):
    def __init__(self, st: dict, message: discord.Message | None):
        super().__init__(timeout=None)
        self.st = st
        self.msg = message  # setupメッセージ

        step = int(st.get("step", 1))
        day_key = st.get("day_key", "today")

        # --- 日付ボタン（選択中ハイライト）
        today_style = discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary
        tomo_style = discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary

        self.add_item(DayButton("今日", "today", today_style))
        self.add_item(DayButton("明日", "tomorrow", tomo_style))
        self.add_item(NextBackButton(step))

        # --- Step1: 時刻選択
        self.add_item(TimeSelect("開始(時)", "start_h", hour_options(), row=1))
        self.add_item(TimeSelect("開始(分)", "start_m", minute_options(5), row=2))
        self.add_item(TimeSelect("終了(時)", "end_h", hour_options(), row=3))
        self.add_item(TimeSelect("終了(分)", "end_m", minute_options(5), row=4))

        # --- Step2: interval/title/everyone/channel + 作成
        if step == 2:
            self.add_item(IntervalSelect())
            self.add_item(TitleButton())
            self.add_item(EveryoneButton())
            self.add_item(NotifyChannelSelect())
            self.add_item(CreateButton())


class DayButton(discord.ui.Button):
    def __init__(self, label: str, val: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style, custom_id=f"setup:day:{val}", row=0)
        self.val = val

    async def callback(self, interaction: discord.Interaction):
        key = dkey(interaction)
        st = draft.get(key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        st["day_key"] = self.val
        # view再生成してハイライト反映
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class NextBackButton(discord.ui.Button):
    def __init__(self, step: int):
        if step == 1:
            super().__init__(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=0)
        else:
            super().__init__(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=0)

    async def callback(self, interaction: discord.Interaction):
        key = dkey(interaction)
        st = draft.get(key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        step = int(st.get("step", 1))
        if step == 1:
            # Step1→2へ（開始/終了は未選択でも移動はOK。作成時に検証）
            st["step"] = 2
        else:
            st["step"] = 1

        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class TimeSelect(discord.ui.Select):
    def __init__(self, placeholder: str, keyname: str, options: list[discord.SelectOption], row: int):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options, row=row)
        self.keyname = keyname
        self.custom_id = f"setup:{keyname}"

    async def callback(self, interaction: discord.Interaction):
        key = dkey(interaction)
        st = draft.get(key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        val = int(self.values[0])
        st[self.keyname] = val

        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class IntervalSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="間隔（20/25/30）",
            min_values=1,
            max_values=1,
            options=interval_options(),
            row=0,
        )
        self.custom_id = "setup:interval_minutes"

    async def callback(self, interaction: discord.Interaction):
        key = dkey(interaction)
        st = draft.get(key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        st["interval_minutes"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class TitleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=1)

    async def callback(self, interaction: discord.Interaction):
        key = dkey(interaction)
        st = draft.get(key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
        await interaction.response.send_modal(TitleModal(st, interaction.message))


class EveryoneButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="@everyone ON/OFF", style=discord.ButtonStyle.danger, custom_id="setup:everyone", row=1)

    async def callback(self, interaction: discord.Interaction):
        key = dkey(interaction)
        st = draft.get(key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        st["mention_everyone"] = not bool(st.get("mention_everyone", False))
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class NotifyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（未選択=このチャンネル）",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        key = dkey(interaction)
        st = draft.get(key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        ch = self.values[0]
        st["notify_channel_id"] = str(ch.id)
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class CreateButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=3)

    async def callback(self, interaction: discord.Interaction):
        key = dkey(interaction)
        st = draft.get(key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        # 必須チェック
        start_hm = hm_from_state(st, "start")
        end_hm = hm_from_state(st, "end")
        interval = st.get("interval_minutes")

        if not start_hm or not end_hm or not interval:
            return await interaction.response.send_message(
                "❌ 開始/終了/間隔 が未選択。/setup からやり直してね",
                ephemeral=True,
            )

        # 日付（JST基準）
        base = datetime.now(JST).date()
        if st.get("day_key") == "tomorrow":
            base = base + timedelta(days=1)

        start_dt = parse_hm_to_datetime(base, start_hm)
        end_dt = parse_hm_to_datetime(base, end_hm)
        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)  # 日跨ぎ対応

        title = st.get("title") or "無題"
        notify_channel_id = st.get("notify_channel_id") or str(interaction.channel_id)
        mention_everyone = bool(st.get("mention_everyone", False))

        # panelsへ保存（start_at/end_at も保存）
        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),
            "day_key": st.get("day_key", "today"),
            "title": title,
            "interval_minutes": int(interval),
            "notify_channel_id": str(notify_channel_id),
            "mention_everyone": mention_everyone,
            "created_by": str(interaction.user.id),
            "created_at": datetime.now(timezone.utc).isoformat(),

            # ここが今回の肝（DB列に合わせる）
            "start_h": int(st["start_h"]),
            "start_m": int(st["start_m"]),
            "end_h": int(st["end_h"]),
            "end_m": int(st["end_m"]),
            "start_hm": start_hm,
            "end_hm": end_hm,
            "start_at": start_dt.astimezone(timezone.utc).isoformat(),
            "end_at": end_dt.astimezone(timezone.utc).isoformat(),
        }

        await interaction.response.defer(ephemeral=True)

        try:
            pres = await db_to_thread(lambda: upsert_panel(row))
        except Exception as e:
            return await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)

        if not pres.data:
            return await interaction.followup.send("❌ 保存できなかった（panelsの制約/列を確認）", ephemeral=True)

        panel = pres.data[0]
        panel_id = int(panel["id"])

        # slots作成（上書き方式：一旦panel_idのslotsを消してから作る）
        try:
            await db_to_thread(lambda: delete_slots(panel_id))
        except Exception:
            # 消せなくても作成は試す
            pass

        slot_rows: list[dict] = []
        cur = start_dt
        while cur < end_dt:
            start_utc = cur.astimezone(timezone.utc)
            end_utc = (cur + timedelta(minutes=int(interval))).astimezone(timezone.utc)
            slot_rows.append(
                {
                    "panel_id": panel_id,
                    "start_at": start_utc.isoformat(),
                    "end_at": end_utc.isoformat(),
                    "slot_time": cur.strftime("%H:%M"),
                    "is_break": False,
                    "reserved_by": None,
                    "notified": False,
                    "reserved_at": None,
                    "reserver_user_id": None,
                    "reserver_name": None,
                }
            )
            cur += timedelta(minutes=int(interval))

        try:
            ins = await db_to_thread(lambda: insert_slots(slot_rows))
        except Exception as e:
            return await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)

        created = ins.data or []
        if not created:
            return await interaction.followup.send("❌ slots が作れなかった（slotsの列/NOT NULL制約を確認）", ephemeral=True)

        # パネル投稿先
        ch = interaction.guild.get_channel(int(notify_channel_id)) or interaction.channel

        # @everyoneは「作成時1回のみ」→ 初回投稿の本文に入れる（編集しても再通知されない）
        prefix = "@everyone " if mention_everyone else ""
        slots_now = created  # 作った直後のデータ
        msg = await ch.send(
            prefix + "募集を開始しました！",
            embed=build_panel_embed(panel, slots_now),
            view=SlotsView(panel_id),
        )

        # message_id保存
        try:
            await db_to_thread(lambda: update_panel_message_id(panel_id, str(msg.id)))
        except Exception:
            pass

        await interaction.followup.send("✅ 保存＆募集パネル投稿まで完了！", ephemeral=True)


# =====================
# Slots View（予約ボタン）
# =====================
class SlotsView(discord.ui.View):
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)
        self.panel_id = panel_id

        # ボタンは最大25個なので「表示」だけ作る（実データは押した時に読む）
        # ここはメッセージ送信時にボタンを作り直すので、最新のslotsを取って作る
        # → ただし View 初期化時点でDBを触れないので、初回は /setup 作成時の send で
        #    SlotsView(panel_id) だけ付けておき、実際のボタン追加は "refresh" でやる方式もある。
        #    今回は簡易のため、slotボタンは panel message を送った直後に "編集" で付け直す必要がある。
        #    なので、このView自体は「slotボタンを押した時の処理だけ」担当にする。
        #
        # ここを完全に動かすため、下で "build_slot_buttons" を呼ぶ。
        self.build_slot_buttons()

    def build_slot_buttons(self):
        # DBは同期で触る（View作成はイベントループ上なのでto_threadしないと重いが、数が少ないので許容）
        res = get_slots(self.panel_id)
        slots = res.data or []

        # 一旦全部消して作り直し
        self.clear_items()

        # 最大 25 個（ここでは 20 個に制限しておく）
        for s in slots[:20]:
            if s.get("is_break"):
                style = discord.ButtonStyle.secondary
                disabled = True
            else:
                style = discord.ButtonStyle.danger if s.get("reserved_by") else discord.ButtonStyle.success
                disabled = False

            label = s.get("slot_time") or slot_label(str(s["start_at"]))
            self.add_item(SlotButton(label=label, slot_id=int(s["id"]), style=style, disabled=disabled))


class SlotButton(discord.ui.Button):
    def __init__(self, label: str, slot_id: int, style: discord.ButtonStyle, disabled: bool):
        super().__init__(label=label, style=style, custom_id=f"slot:{slot_id}", disabled=disabled)
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 1) slot取得
        try:
            r = await db_to_thread(lambda: get_slot(self.slot_id))
        except Exception as e:
            return await interaction.followup.send(f"❌ 読み込み失敗: {e}", ephemeral=True)

        if not r.data:
            return await interaction.followup.send("❌ 枠が見つからない", ephemeral=True)

        slot = r.data[0]
        if slot.get("is_break"):
            return await interaction.followup.send("❌ 休憩枠です", ephemeral=True)

        reserved_by = slot.get("reserved_by")
        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name

        # 2) 予約/キャンセル
        try:
            if reserved_by:
                if str(reserved_by) != user_id:
                    return await interaction.followup.send("❌ 他人の予約はキャンセルできない", ephemeral=True)
                await db_to_thread(lambda: cancel_slot(self.slot_id))
                msg_text = "✅ キャンセルしたよ！"
            else:
                await db_to_thread(lambda: reserve_slot(self.slot_id, user_id, user_name))
                msg_text = "✅ 予約したよ！"
        except Exception as e:
            return await interaction.followup.send(f"❌ 更新失敗: {e}", ephemeral=True)

        # 3) パネルを即更新（embed & ボタン色）
        try:
            panel_id = int(slot["panel_id"])
            # panels取得（day_key等を使う）
            pres = await db_to_thread(lambda: sb.table("panels").select("*").eq("id", panel_id).limit(1).execute())
            panel = pres.data[0] if pres.data else {"title": "募集パネル", "interval_minutes": 30, "day_key": "today"}

            sres = await db_to_thread(lambda: get_slots(panel_id))
            slots = sres.data or []

            # 送信元メッセージを更新（interaction.message がパネル本体）
            view = SlotsView(panel_id)  # 最新状態でボタン再生成
            await interaction.message.edit(embed=build_panel_embed(panel, slots), view=view)
        except Exception:
            # 更新失敗しても予約自体は成功してるので握る
            pass

        await interaction.followup.send(msg_text, ephemeral=True)


# =====================
# Commands
# =====================
@tree.command(name="setup", description="募集パネルを作る（ウィザード）")
async def setup_cmd(interaction: discord.Interaction):
    key = dkey(interaction)
    draft[key] = {
        "step": 1,
        "day_key": "today",   # 初期は今日
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
    }
    st = draft[key]

    # まず仮で送信→そのメッセージを view に渡す（edit_messageに使う）
    await interaction.response.send_message("ボタン/セレクトで設定してね👇", embed=build_setup_embed(st), ephemeral=False)
    msg = await interaction.original_response()
    await msg.edit(embed=build_setup_embed(st), view=SetupView(st, msg))


# =====================
# lifecycle
# =====================
@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


async def main():
    # 429対策（必要なら増やしてOK）
    await asyncio.sleep(5)
    await client.start(TOKEN)


asyncio.run(main())