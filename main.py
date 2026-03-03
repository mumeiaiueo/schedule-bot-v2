import os
import asyncio
from datetime import datetime, timedelta, timezone, date

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

# key: (guild_id, user_id) -> state
draft: dict[tuple[str, str], dict] = {}

def dkey(interaction: discord.Interaction):
    return (str(interaction.guild_id), str(interaction.user.id))

async def db_to_thread(fn):
    return await asyncio.to_thread(fn)

# ========= DB helpers =========
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

def update_panel_message_id(panel_id: int, message_id: str):
    return sb.table("panels").update({"panel_message_id": message_id}).eq("id", panel_id).execute()

def delete_panel(guild_id: str, day_key: str):
    return sb.table("panels").delete().eq("guild_id", guild_id).eq("day_key", day_key).execute()

def delete_slots_by_panel(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()

def insert_slots(rows: list[dict]):
    return sb.table("slots").insert(rows).execute()

def fetch_slots(panel_id: int):
    # slot_time で並べたいなら start_at でOK
    return sb.table("slots").select("*").eq("panel_id", panel_id).order("start_at").execute()

def update_slot_reserved(slot_id: int, reserved_by: str | None):
    return sb.table("slots").update({"reserved_by": reserved_by}).eq("id", slot_id).execute()

# ========= time helpers =========
def hm(h: int | None, m: int | None):
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"

def parse_int(s: str) -> int:
    return int(s)

def today_jst() -> date:
    return datetime.now(JST).date()

def make_dt(day: date, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=JST)

def slot_label(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%H:%M")

# ========= UI options =========
def hour_options(selected: int | None):
    opts = []
    for h in range(24):
        opts.append(discord.SelectOption(label=f"{h:02d}", value=str(h), default=(selected == h)))
    return opts

def minute_options(selected: int | None, step=5):
    opts = []
    for m in range(0, 60, step):
        opts.append(discord.SelectOption(label=f"{m:02d}", value=str(m), default=(selected == m)))
    return opts

def interval_options(selected: int | None):
    items = [20, 25, 30]
    return [discord.SelectOption(label=f"{v}分", value=str(v), default=(selected == v)) for v in items]

# ========= Embed builders =========
def build_setup_embed(st: dict) -> discord.Embed:
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)

    step = st.get("step", 1)
    day_key = st.get("day_key", "today")
    day_label = "今日" if day_key == "today" else "明日"

    start_h = st.get("start_h")
    start_m = st.get("start_m")
    end_h = st.get("end_h")
    end_m = st.get("end_m")

    start_hm = hm(start_h, start_m) or "未選択"
    end_hm = hm(end_h, end_m) or "未選択"

    interval = st.get("interval_minutes")
    interval_label = f"{interval}分" if interval else "未選択"

    title = st.get("title") or "無題"
    notify = st.get("notify_channel_id")
    notify_label = f"<#{notify}>" if notify else "このチャンネル"

    everyone = bool(st.get("mention_everyone", False))
    everyone_label = "ON" if everyone else "OFF"

    e.add_field(name="Step", value=str(step), inline=False)
    e.add_field(name="日付", value=day_label, inline=True)
    e.add_field(name="開始", value=start_hm, inline=True)
    e.add_field(name="終了", value=end_hm, inline=True)

    if step >= 2:
        e.add_field(name="間隔", value=interval_label, inline=True)
        e.add_field(name="タイトル", value=title, inline=False)
        e.add_field(name="通知チャンネル", value=notify_label, inline=False)
        e.add_field(name="@everyone", value=everyone_label, inline=True)

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e

def build_panel_embed(panel: dict, slots: list[dict]) -> discord.Embed:
    day_key = panel.get("day_key", "today")
    day_label = "今日" if day_key == "today" else "明日"
    interval = panel.get("interval_minutes", 0)
    title = panel.get("title", "募集パネル")

    e = discord.Embed(title="募集パネル", color=0x2B2D31)
    e.add_field(name="日付", value=f"{day_label}（JST） / interval {interval}min", inline=False)

    lines = []
    for r in slots:
        t = r.get("slot_time") or "??:??"
        is_break = bool(r.get("is_break", False))
        reserved_by = r.get("reserved_by")

        if is_break:
            lines.append(f"⚪ {t} 休憩")
        elif reserved_by:
            lines.append(f"🔴 {t} <@{reserved_by}>")
        else:
            lines.append(f"🟢 {t}")

    e.add_field(
        name="枠",
        value="\n".join(lines) if lines else "（まだ枠がありません）",
        inline=False
    )

    e.add_field(
        name="凡例",
        value="🟢空き / 🔴予約済み（本人は押すとキャンセル） / ⚪休憩（予約不可）",
        inline=False
    )
    return e

# ========= Views =========
class SetupView(discord.ui.View):
    def __init__(self, st: dict):
        super().__init__(timeout=None)
        self.st = st

        # Step 1: day + start/end + next
        # Step 2: interval/title/everyone/channel + back + create

        step = st.get("step", 1)
        day_key = st.get("day_key", "today")

        # --- Day buttons (always visible) ---
        self.add_item(SetupDayButton("今日", "today", primary=(day_key == "today"), row=0))
        self.add_item(SetupDayButton("明日", "tomorrow", primary=(day_key == "tomorrow"), row=0))

        if step == 1:
            # start/end selects
            self.add_item(SetupSelect("setup:start_h", "開始(時)", hour_options(st.get("start_h")), row=1))
            self.add_item(SetupSelect("setup:start_m", "開始(分)", minute_options(st.get("start_m"), 5), row=2))
            self.add_item(SetupSelect("setup:end_h", "終了(時)", hour_options(st.get("end_h")), row=3))
            self.add_item(SetupSelect("setup:end_m", "終了(分)", minute_options(st.get("end_m"), 5), row=4))

            ok = (st.get("start_h") is not None and st.get("start_m") is not None and
                  st.get("end_h") is not None and st.get("end_m") is not None)
            self.add_item(SetupNavButton(label="次へ", go_step=2, disabled=(not ok), row=0))

        else:
            # interval + title + everyone + channel select + back/create
            self.add_item(SetupSelect("setup:interval_minutes", "間隔（20/25/30）", interval_options(st.get("interval_minutes")), row=1))
            self.add_item(SetupTitleButton(row=2))
            self.add_item(SetupEveryoneButton(row=2))

            cs = discord.ui.ChannelSelect(
                custom_id="setup:notify_channel",
                placeholder="通知チャンネル（未選択=このチャンネル）",
                min_values=1,
                max_values=1,
                channel_types=[discord.ChannelType.text],
                row=3
            )
            self.add_item(cs)

            self.add_item(SetupNavButton(label="戻る", go_step=1, style=discord.ButtonStyle.secondary, row=4))
            self.add_item(SetupCreateButton(row=4))

class SetupDayButton(discord.ui.Button):
    def __init__(self, label: str, value: str, primary: bool, row: int):
        super().__init__(
            label=label,
            style=(discord.ButtonStyle.primary if primary else discord.ButtonStyle.secondary),
            custom_id=f"setup:day:{value}",
            row=row
        )
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        st["day_key"] = self.value

        await interaction.response.edit_message(
            embed=build_setup_embed(st),
            view=SetupView(st)
        )

class SetupSelect(discord.ui.Select):
    def __init__(self, cid: str, placeholder: str, options: list[discord.SelectOption], row: int):
        super().__init__(custom_id=cid, placeholder=placeholder, options=options, min_values=1, max_values=1, row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return

        cid = self.custom_id
        val = self.values[0]

        if cid == "setup:start_h":
            st["start_h"] = parse_int(val)
        elif cid == "setup:start_m":
            st["start_m"] = parse_int(val)
        elif cid == "setup:end_h":
            st["end_h"] = parse_int(val)
        elif cid == "setup:end_m":
            st["end_m"] = parse_int(val)
        elif cid == "setup:interval_minutes":
            st["interval_minutes"] = parse_int(val)

        await interaction.response.edit_message(
            embed=build_setup_embed(st),
            view=SetupView(st)
        )

class SetupTitleButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        await interaction.response.send_modal(TitleModal(st))

class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False)

    def __init__(self, st: dict):
        super().__init__(timeout=300)
        self.st = st

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"

        # 反映：元メッセージを更新（Modalからでも edit_message 可能）
        await interaction.response.edit_message(
            embed=build_setup_embed(self.st),
            view=SetupView(self.st)
        )

class SetupEveryoneButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="@everyone ON/OFF", style=discord.ButtonStyle.danger, custom_id="setup:everyone", row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        st["mention_everyone"] = not bool(st.get("mention_everyone", False))
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st))

class SetupNavButton(discord.ui.Button):
    def __init__(self, label: str, go_step: int, disabled: bool = False, style=discord.ButtonStyle.success, row: int = 0):
        super().__init__(label=label, style=style, custom_id=f"setup:step:{go_step}", disabled=disabled, row=row)
        self.go_step = go_step

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return
        st["step"] = self.go_step
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st))

class SetupCreateButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
            return

        # 必須チェック
        need = ["start_h", "start_m", "end_h", "end_m", "interval_minutes"]
        if any(st.get(k) is None for k in need):
            await interaction.response.send_message("❌ 未選択があります（開始/終了/間隔）", ephemeral=True)
            return

        # チャンネルセレクト（ChannelSelect）は interaction.data["resolved"] になったりするが、
        # discord.py側が self.view.children の ChannelSelect に values を入れてくれる
        notify_channel_id = None
        for child in self.view.children:
            if isinstance(child, discord.ui.ChannelSelect) and child.custom_id == "setup:notify_channel":
                if child.values:
                    notify_channel_id = str(child.values[0].id)

        if notify_channel_id:
            st["notify_channel_id"] = notify_channel_id

        # 保存 row（panelsの実在カラムだけ）
        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),
            "day_key": st.get("day_key", "today"),
            "title": st.get("title") or "無題",
            "interval_minutes": int(st["interval_minutes"]),
            "notify_channel_id": st.get("notify_channel_id"),
            "mention_everyone": bool(st.get("mention_everyone", False)),
            "created_by": str(interaction.user.id),
            "created_at": datetime.now(timezone.utc).isoformat(),

            "start_h": int(st["start_h"]),
            "start_m": int(st["start_m"]),
            "end_h": int(st["end_h"]),
            "end_m": int(st["end_m"]),
            "start_hm": hm(st["start_h"], st["start_m"]),
            "end_hm": hm(st["end_h"], st["end_m"]),
        }

        await interaction.response.defer(ephemeral=True)
        try:
            await db_to_thread(lambda: upsert_panel(row))
        except Exception as e:
            await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
            return

        await interaction.followup.send("✅ 保存できた！次は /generate で枠ボタン生成してね", ephemeral=True)

# ========= Slots message view =========
class SlotButton(discord.ui.Button):
    def __init__(self, label: str, slot_id: int, style: discord.ButtonStyle):
        super().__init__(label=label, style=style, custom_id=f"slot:{slot_id}")
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        # 現在の予約者
        def work_get():
            return sb.table("slots").select("reserved_by,is_break").eq("id", self.slot_id).limit(1).execute()
        res = await db_to_thread(work_get)
        if not res.data:
            await interaction.followup.send("❌ その枠が見つからない", ephemeral=True)
            return

        row = res.data[0]
        if bool(row.get("is_break", False)):
            await interaction.followup.send("❌ 休憩枠は予約できない", ephemeral=True)
            return

        current = row.get("reserved_by")

        # 他人予約は不可
        if current and str(current) != user_id:
            await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
            return

        # 同じ人が押したらキャンセル
        new_val = None if current else user_id

        try:
            await db_to_thread(lambda: update_slot_reserved(self.slot_id, new_val))
        except Exception as e:
            await interaction.followup.send(f"❌ 更新失敗: {e}", ephemeral=True)
            return

        await interaction.followup.send("✅ 予約したよ！" if new_val else "✅ キャンセルしたよ！", ephemeral=True)

        # パネルを更新（messageのembed/viewを作り直す）
        # message_id から panel_id を逆引きできないので、同チャンネルの直近メッセージを編集は危険。
        # ここは「slot_id→panel_id」を取得して、そのpanelのpanel_message_idを読んで更新する。
        def work_panel():
            s = sb.table("slots").select("panel_id").eq("id", self.slot_id).limit(1).execute()
            if not s.data:
                return None
            pid = s.data[0]["panel_id"]
            p = sb.table("panels").select("*").eq("id", pid).limit(1).execute()
            if not p.data:
                return None
            return p.data[0]

        panel = await db_to_thread(work_panel)
        if not panel:
            return

        panel_id = panel["id"]
        msg_id = panel.get("panel_message_id")
        if not msg_id:
            return

        slots_res = await db_to_thread(lambda: fetch_slots(panel_id))
        slots = slots_res.data or []

        # 更新対象メッセージを取得して編集
        ch_id = panel.get("notify_channel_id") or panel.get("channel_id")
        try:
            ch = interaction.guild.get_channel(int(ch_id))
            if ch is None:
                return
            msg = await ch.fetch_message(int(msg_id))
            await msg.edit(embed=build_panel_embed(panel, slots), view=SlotsView(panel, slots))
        except Exception:
            pass

class SlotsView(discord.ui.View):
    def __init__(self, panel: dict, slots: list[dict]):
        super().__init__(timeout=None)

        # Discordは1メッセージ最大25コンポーネント
        # まずは最大20枠まで
        for r in slots[:20]:
            sid = r["id"]
            label = r.get("slot_time") or "??:??"
            is_break = bool(r.get("is_break", False))
            reserved_by = r.get("reserved_by")

            if is_break:
                style = discord.ButtonStyle.secondary
            elif reserved_by:
                style = discord.ButtonStyle.danger
            else:
                style = discord.ButtonStyle.success

            self.add_item(SlotButton(label=label, slot_id=sid, style=style))

# ========= commands =========
@tree.command(name="setup", description="募集パネルを作る（ウィザード）")
async def setup(interaction: discord.Interaction):
    st = {
        "step": 1,
        "day_key": "today",  # 初期は今日
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
    }
    draft[dkey(interaction)] = st
    await interaction.response.send_message(
        "ボタン/セレクトで設定してね👇",
        embed=build_setup_embed(st),
        view=SetupView(st),
        ephemeral=False
    )

@tree.command(name="generate", description="保存済みパネル設定から枠を生成して投稿")
@app_commands.describe(day="today / tomorrow（省略=today）")
async def generate(interaction: discord.Interaction, day: str = "today"):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id)
    day_key = "tomorrow" if day.lower() in ("tomorrow", "明日") else "today"

    # panels取得
    pres = await db_to_thread(lambda: get_panel(guild_id, day_key))
    if not pres.data:
        await interaction.followup.send("❌ 先に /setup → 作成 をしてね", ephemeral=True)
        return
    panel = pres.data[0]
    panel_id = panel["id"]

    # 必須
    start_h = panel.get("start_h")
    start_m = panel.get("start_m")
    end_h = panel.get("end_h")
    end_m = panel.get("end_m")
    interval = int(panel.get("interval_minutes", 0))
    if start_h is None or start_m is None or end_h is None or end_m is None or interval <= 0:
        await interaction.followup.send("❌ 開始/終了/間隔が保存されてない。/setup からやり直してね", ephemeral=True)
        return

    notify_channel_id = panel.get("notify_channel_id") or str(interaction.channel_id)
    title = panel.get("title", "募集パネル")
    mention_everyone = bool(panel.get("mention_everyone", False))

    # 枠生成（今日/明日 + 日跨ぎ対応）
    base = today_jst() + (timedelta(days=1) if day_key == "tomorrow" else timedelta(days=0))
    start_dt = make_dt(base, int(start_h), int(start_m))
    end_dt = make_dt(base, int(end_h), int(end_m))
    if end_dt <= start_dt:
        end_dt = end_dt + timedelta(days=1)  # 日跨ぎ

    # 既存slots削除（重複ユニーク対策）
    try:
        await db_to_thread(lambda: delete_slots_by_panel(panel_id))
    except Exception as e:
        await interaction.followup.send(f"❌ slots削除失敗: {e}", ephemeral=True)
        return

    rows = []
    cur = start_dt
    while cur < end_dt:
        nxt = cur + timedelta(minutes=interval)
        rows.append({
            "panel_id": panel_id,
            "start_at": cur.astimezone(timezone.utc).isoformat(),
            "end_at": nxt.astimezone(timezone.utc).isoformat(),
            "slot_time": slot_label(cur),         # ★ slots.slot_time NOT NULL 対策
            "is_break": False,
            "reserved_by": None,
            "notified": False,
            "reserver_user_id": None,
            "reserver_name": None,
            "reserved_at": None,
        })
        cur = nxt

    ins = await db_to_thread(lambda: insert_slots(rows))
    created = ins.data or []
    if not created:
        await interaction.followup.send("❌ slots が作れなかった（slots列が足りない可能性）", ephemeral=True)
        return

    # 投稿
    ch = interaction.guild.get_channel(int(notify_channel_id)) or interaction.channel

    # @everyone は「作成時1回」想定。まずは generate 実行時に1回送るだけ。
    if mention_everyone:
        try:
            await ch.send("@everyone 募集を開始しました！")
        except Exception:
            pass

    slots_res = await db_to_thread(lambda: fetch_slots(panel_id))
    slots = slots_res.data or []

    msg = await ch.send(
        "下のボタンで予約してね👇",
        embed=build_panel_embed(panel, slots),
        view=SlotsView(panel, slots)
    )

    # panelsに message_id 保存
    try:
        await db_to_thread(lambda: update_panel_message_id(panel_id, str(msg.id)))
    except Exception:
        pass

    await interaction.followup.send("✅ 枠ボタンを生成して投稿した！", ephemeral=True)

@tree.command(name="reset", description="今日/明日の募集を削除（panels+slots）")
@app_commands.describe(day="today / tomorrow（省略=today）")
async def reset(interaction: discord.Interaction, day: str = "today"):
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild_id)
    day_key = "tomorrow" if day.lower() in ("tomorrow", "明日") else "today"

    pres = await db_to_thread(lambda: get_panel(guild_id, day_key))
    if pres.data:
        panel = pres.data[0]
        panel_id = panel["id"]
        try:
            await db_to_thread(lambda: delete_slots_by_panel(panel_id))
        except Exception:
            pass

    try:
        await db_to_thread(lambda: delete_panel(guild_id, day_key))
    except Exception as e:
        await interaction.followup.send(f"❌ reset失敗: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ {('明日' if day_key=='tomorrow' else '今日')} の募集を削除したよ", ephemeral=True)

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

async def main():
    # 再起動連打で429になりやすいので少し待つ
    await asyncio.sleep(5)
    await client.start(TOKEN)

asyncio.run(main())