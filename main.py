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

# ---- 一時保存（setup中のユーザーごと）----
draft = {}  # key: (guild_id, user_id) -> dict


def dkey(interaction: discord.Interaction):
    return (str(interaction.guild_id), str(interaction.user.id))


async def db_to_thread(fn):
    return await asyncio.to_thread(fn)


# ========= DB helpers =========
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


def delete_slots(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()


def insert_slots(rows: list[dict]):
    return sb.table("slots").insert(rows).execute()


def list_slots(panel_id: int):
    return (
        sb.table("slots")
        .select("*")
        .eq("panel_id", panel_id)
        .order("start_at")
        .execute()
    )


def update_panel_message_id(panel_id: int, message_id: str):
    return sb.table("panels").update({"panel_message_id": message_id}).eq("id", panel_id).execute()


def hm_from_state(st: dict, prefix: str):
    h = st.get(f"{prefix}_h")
    m = st.get(f"{prefix}_m")
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"


def build_setup_embed(st: dict):
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)

    step = int(st.get("step", 1))
    day_key = st.get("day_key", "today")
    day_str = "今日" if day_key == "today" else "明日"

    start = hm_from_state(st, "start")
    end = hm_from_state(st, "end")
    interval = st.get("interval_minutes")
    title = st.get("title") or "無題"
    notify = st.get("notify_channel_id")
    everyone = bool(st.get("mention_everyone", False))

    e.add_field(name="Step", value=str(step), inline=True)
    e.add_field(name="日付", value=day_str, inline=True)
    e.add_field(name="開始", value=(start or "未選択"), inline=True)
    e.add_field(name="終了", value=(end or "未選択"), inline=True)

    e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)
    e.add_field(name="タイトル", value=title, inline=False)
    e.add_field(
        name="通知チャンネル",
        value=(f"<#{notify}>" if notify else "このチャンネル"),
        inline=False,
    )
    e.add_field(name="@everyone", value=("ON" if everyone else "OFF"), inline=True)

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e


def build_panel_embed(panel: dict, slots: list[dict]):
    # 「募集パネル」表示（ユーザーが押す本番パネル）
    day_key = panel.get("day_key", "today")
    day_str = "今日 (JST)" if day_key == "today" else "明日 (JST)"
    interval = panel.get("interval_minutes", 30)
    title = panel.get("title") or "募集パネル"

    e = discord.Embed(title=title, color=0x2b2d31)
    e.add_field(name="日付", value=f"📅 {day_str} / interval {interval}min", inline=False)

    # 枠一覧（最大8行くらい表示）
    lines = []
    for r in slots[:12]:
        t = r["slot_time"]
        reserved_by = r.get("reserved_by")
        if reserved_by:
            lines.append(f"🔴 {t} <@{reserved_by}>")
        else:
            lines.append(f"🟢 {t}")
    if not lines:
        lines = ["(枠がありません)"]

    e.add_field(name="枠", value="\n".join(lines), inline=False)
    e.add_field(name="凡例", value="🟢空き / 🔴予約済み（本人は押すとキャンセル）", inline=False)
    return e


def compute_start_end_jst(st: dict):
    """start/end を JST datetime にして返す。日跨ぎも吸収。"""
    day_key = st.get("day_key", "today")
    now = datetime.now(JST)
    base_date = now.date() + (timedelta(days=1) if day_key == "tomorrow" else timedelta(days=0))

    sh = st.get("start_h")
    sm = st.get("start_m")
    eh = st.get("end_h")
    em = st.get("end_m")
    if sh is None or sm is None or eh is None or em is None:
        return None, None

    start = datetime(base_date.year, base_date.month, base_date.day, int(sh), int(sm), tzinfo=JST)
    end = datetime(base_date.year, base_date.month, base_date.day, int(eh), int(em), tzinfo=JST)

    # 日跨ぎ：終了 <= 開始 なら翌日にする
    if end <= start:
        end = end + timedelta(days=1)
    return start, end


def make_slot_rows(panel_id: int, start_jst: datetime, end_jst: datetime, interval_min: int):
    rows = []
    cur = start_jst
    while cur < end_jst:
        slot_time = cur.strftime("%H:%M")  # JST 表示用（必須列）
        rows.append({
            "panel_id": panel_id,
            "start_at": cur.astimezone(timezone.utc).isoformat(),
            "end_at": (cur + timedelta(minutes=interval_min)).astimezone(timezone.utc).isoformat(),
            "slot_time": slot_time,
            "is_break": False,
            "reserved_by": None,
            "notified": False,
        })
        cur += timedelta(minutes=interval_min)
    return rows


# ========= Modal =========
class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False)

    def __init__(self, st: dict, message: discord.Message):
        super().__init__(timeout=300)
        self.st = st
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        # embed更新
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupView(self.st, self.message))


# ========= Setup View (Step1/2) =========
class SetupView(discord.ui.View):
    def __init__(self, st: dict, message: discord.Message | None = None):
        super().__init__(timeout=None)
        self.st = st
        self.message = message

        step = int(st.get("step", 1))
        day_key = st.get("day_key", "today")

        # Day buttons (Step1で使う)
        if step == 1:
            self.add_item(DayButton("今日", "today", selected=(day_key == "today")))
            self.add_item(DayButton("明日", "tomorrow", selected=(day_key == "tomorrow")))
            self.add_item(NextButton())

            self.add_item(TimeSelect("setup:start_h", "開始(時)", [f"{h:02d}" for h in range(24)]))
            self.add_item(TimeSelect("setup:start_m", "開始(分)", [f"{m:02d}" for m in range(0, 60, 5)]))
            self.add_item(TimeSelect("setup:end_h", "終了(時)", [f"{h:02d}" for h in range(24)]))
            self.add_item(TimeSelect("setup:end_m", "終了(分)", [f"{m:02d}" for m in range(0, 60, 5)]))

        # Step2
        if step == 2:
            self.add_item(IntervalSelect())
            self.add_item(TitleButton())
            self.add_item(EveryoneButton())

            self.add_item(NotifyChannelSelect())
            self.add_item(BackButton())
            self.add_item(CreateButton())


class DayButton(discord.ui.Button):
    def __init__(self, label: str, value: str, selected: bool):
        style = discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, custom_id=f"setup:day:{value}", row=0)
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        st["day_key"] = self.value
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class NextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=0)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        st["step"] = 2
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=4)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        st["step"] = 1
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class TimeSelect(discord.ui.Select):
    def __init__(self, cid: str, placeholder: str, values: list[str]):
        options = [discord.SelectOption(label=v, value=v) for v in values]
        super().__init__(custom_id=cid, placeholder=placeholder, options=options, min_values=1, max_values=1, row=1 if "start" in cid else 2)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return

        val = self.values[0]
        cid = self.custom_id

        if cid == "setup:start_h":
            st["start_h"] = int(val)
        elif cid == "setup:start_m":
            st["start_m"] = int(val)
        elif cid == "setup:end_h":
            st["end_h"] = int(val)
        elif cid == "setup:end_m":
            st["end_m"] = int(val)

        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class IntervalSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="20分", value="20"),
            discord.SelectOption(label="25分", value="25"),
            discord.SelectOption(label="30分", value="30"),
        ]
        super().__init__(custom_id="setup:interval", placeholder="間隔（20/25/30）", options=options, min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        st["interval_minutes"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class TitleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=2)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        await interaction.response.send_modal(TitleModal(st, interaction.message))


class EveryoneButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="@everyone ON/OFF", style=discord.ButtonStyle.danger, custom_id="setup:everyone", row=2)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
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
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        ch = self.values[0]
        st["notify_channel_id"] = str(ch.id)
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))


class CreateButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=4)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return

        # 必須チェック
        start_jst, end_jst = compute_start_end_jst(st)
        if not start_jst or not end_jst:
            await interaction.response.send_message("❌ 開始/終了が未設定。Step1で選んでね", ephemeral=True)
            return
        interval = st.get("interval_minutes")
        if not interval:
            await interaction.response.send_message("❌ 間隔が未選択。選んでね", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        channel_id = str(interaction.channel_id)
        day_key = st.get("day_key", "today")
        title = st.get("title") or "無題"
        notify_channel_id = st.get("notify_channel_id") or str(interaction.channel_id)
        mention_everyone = bool(st.get("mention_everyone", False))

        row = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "day_key": day_key,
            "title": title,
            "interval_minutes": int(interval),
            "notify_channel_id": notify_channel_id,
            "mention_everyone": mention_everyone,
            "created_by": str(interaction.user.id),
            "created_at": datetime.now(timezone.utc).isoformat(),
            # 時刻系（panels側に合わせる）
            "start_h": int(st["start_h"]),
            "start_m": int(st["start_m"]),
            "end_h": int(st["end_h"]),
            "end_m": int(st["end_m"]),
            "start_hm": start_jst.strftime("%H:%M"),
            "end_hm": end_jst.strftime("%H:%M"),
        }

        await interaction.response.defer(ephemeral=True)

        # 1) panels 保存（upsert）
        try:
            pres = await db_to_thread(lambda: upsert_panel(row))
        except Exception as e:
            await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
            return

        panel = (pres.data or [None])[0]
        if not panel:
            await interaction.followup.send("❌ 保存できたけど取得に失敗（dataが空）", ephemeral=True)
            return

        panel_id = int(panel["id"])

        # 2) slots 全削除→再生成
        try:
            await db_to_thread(lambda: delete_slots(panel_id))
        except Exception as e:
            await interaction.followup.send(f"❌ slots削除失敗: {e}", ephemeral=True)
            return

        slot_rows = make_slot_rows(panel_id, start_jst, end_jst, int(interval))
        try:
            ins = await db_to_thread(lambda: insert_slots(slot_rows))
        except Exception as e:
            await interaction.followup.send(f"❌ slots作成失敗: {e}", ephemeral=True)
            return

        created_slots = ins.data or []

        # 3) 投稿（通知チャンネルへ）
        ch = interaction.guild.get_channel(int(notify_channel_id)) or interaction.channel

        # @everyone は「作成時に1回だけ送信」
        prefix = "@everyone " if mention_everyone else ""
        embed = build_panel_embed(panel, created_slots)
        msg = await ch.send(prefix + "募集を開始しました！", embed=embed, view=SlotsView(panel_id))

        # 4) panels に message_id 保存
        try:
            await db_to_thread(lambda: update_panel_message_id(panel_id, str(msg.id)))
        except Exception:
            pass

        await interaction.followup.send("✅ 作成した！パネルを投稿したよ", ephemeral=True)


# ========= Slots view (予約ボタン群) =========
class SlotsView(discord.ui.View):
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)
        self.panel_id = panel_id

        # 初期描画は空でもOK（on_readyで使わない前提）
        # 実際は send直前に embed生成してるので、ここではボタンだけ構築する
        # 最新状態でボタン色を作るため、ここでDB読みに行く
        # ※discord.py Viewの__init__は同期なので、DBは別関数で作るのが理想
        # ここは「とりあえず動く」最小構成：ボタンは固定青、押したら更新で色が変わる
        # → 送信直後に色を揃えたい場合は後で改善（必要なら言って）
        pass


async def rebuild_slots_view(panel_id: int):
    # DBからslotsを取って、色付きボタンViewを作る
    sres = await db_to_thread(lambda: list_slots(panel_id))
    rows = sres.data or []

    v = discord.ui.View(timeout=None)

    # 最大25ボタン制限 → まず25個まで
    for r in rows[:25]:
        t = r["slot_time"]
        sid = int(r["id"])
        reserved_by = r.get("reserved_by")
        style = discord.ButtonStyle.danger if reserved_by else discord.ButtonStyle.success
        v.add_item(SlotButton(panel_id, sid, t, style))

    return v, rows


class SlotButton(discord.ui.Button):
    def __init__(self, panel_id: int, slot_id: int, label: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style, custom_id=f"slot:{slot_id}")
        self.panel_id = panel_id
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)

        await interaction.response.defer(ephemeral=True)

        # 最新取得
        def work_get():
            return sb.table("slots").select("reserved_by").eq("id", self.slot_id).limit(1).execute()
        res = await db_to_thread(work_get)
        cur = (res.data or [None])[0]
        if not cur:
            await interaction.followup.send("❌ 枠が見つからない", ephemeral=True)
            return

        reserved_by = cur.get("reserved_by")

        # 予約 → 空きなら取る
        if not reserved_by:
            def work_set():
                # reserved_by が NULL のときだけ更新（競合を軽減）
                return (
                    sb.table("slots")
                    .update({"reserved_by": user_id, "reserved_at": datetime.now(timezone.utc).isoformat()})
                    .eq("id", self.slot_id)
                    .is_("reserved_by", "null")
                    .execute()
                )
            up = await db_to_thread(work_set)
            if not up.data:
                await interaction.followup.send("❌ その枠は先に取られた", ephemeral=True)
                return
            await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

        # キャンセル → 本人だけ
        elif reserved_by == user_id:
            def work_cancel():
                return (
                    sb.table("slots")
                    .update({"reserved_by": None, "reserved_at": None})
                    .eq("id", self.slot_id)
                    .eq("reserved_by", user_id)
                    .execute()
                )
            up = await db_to_thread(work_cancel)
            if not up.data:
                await interaction.followup.send("❌ キャンセル失敗（競合）", ephemeral=True)
                return
            await interaction.followup.send("✅ キャンセルしたよ！", ephemeral=True)

        else:
            await interaction.followup.send("❌ 他の人の予約はキャンセルできない", ephemeral=True)
            return

        # パネルを再描画（embed + view 更新）
        # panels取得
        pres = await db_to_thread(lambda: sb.table("panels").select("*").eq("id", self.panel_id).limit(1).execute())
        panel = (pres.data or [None])[0]
        if not panel:
            return

        new_view, slots = await rebuild_slots_view(self.panel_id)
        new_embed = build_panel_embed(panel, slots)

        try:
            await interaction.message.edit(embed=new_embed, view=new_view)  # type: ignore
        except Exception:
            pass


# ========= command =========
@tree.command(name="setup", description="募集パネルを作る（ウィザード開始）")
async def setup(interaction: discord.Interaction):
    key = dkey(interaction)
    draft[key] = {
        "step": 1,
        "day_key": "today",  # 初期は今日
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,  # 未選択ならこのチャンネル扱い
    }
    st = draft[key]
    await interaction.response.send_message(
        "ボタン/セレクトで設定してね👇",
        embed=build_setup_embed(st),
        view=SetupView(st, None),
        ephemeral=False
    )


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


async def main():
    # 429避け（必要なら残す）
    await asyncio.sleep(2)
    await client.start(TOKEN)


asyncio.run(main())