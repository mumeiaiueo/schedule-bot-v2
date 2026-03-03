import os
import asyncio
from dataclasses import dataclass
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
UTC = timezone.utc

# ========= discord =========
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ========= draft state (ephemeral) =========
draft = {}  # key: (guild_id, user_id) -> state dict


def dkey(interaction: discord.Interaction):
    return (str(interaction.guild_id), str(interaction.user.id))


async def db_to_thread(fn):
    return await asyncio.to_thread(fn)


# ========= DB helpers =========
def db_upsert_panel(row: dict):
    # panels に (guild_id, day_key) unique がある前提
    return sb.table("panels").upsert(row, on_conflict="guild_id,day_key").execute()


def db_get_panel(guild_id: str, day_key: str):
    return (
        sb.table("panels")
        .select("*")
        .eq("guild_id", guild_id)
        .eq("day_key", day_key)
        .limit(1)
        .execute()
    )


def db_update_panel(panel_id: int, patch: dict):
    return sb.table("panels").update(patch).eq("id", panel_id).execute()


def db_delete_slots(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()


def db_insert_slots(rows: list[dict]):
    # 重複対策は「deleteしてからinsert」に寄せる
    return sb.table("slots").insert(rows).execute()


def db_list_slots(panel_id: int):
    return (
        sb.table("slots")
        .select("*")
        .eq("panel_id", panel_id)
        .order("start_at")
        .execute()
    )


def db_get_slot(slot_id: int):
    return sb.table("slots").select("*").eq("id", slot_id).limit(1).execute()


def db_update_slot(slot_id: int, patch: dict):
    return sb.table("slots").update(patch).eq("id", slot_id).execute()


# ========= time helpers =========
def jst_now() -> datetime:
    return datetime.now(JST)


def day_to_date(day_key: str) -> date:
    today = jst_now().date()
    return today if day_key == "today" else (today + timedelta(days=1))


def build_dt(day_key: str, hour: int, minute: int) -> datetime:
    d = day_to_date(day_key)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=JST)


def fmt_hm(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%H:%M")


def fmt_day_label(day_key: str) -> str:
    return "今日" if day_key == "today" else "明日"


# ========= UI options =========
def hour_options_start():
    # 00-23
    return [discord.SelectOption(label=f"{h:02d}", value=str(h)) for h in range(24)]


def hour_options_end():
    # 00-24（24:00用）
    return [discord.SelectOption(label=f"{h:02d}", value=str(h)) for h in range(25)]


def minute_options(step=5):
    return [discord.SelectOption(label=f"{m:02d}", value=str(m)) for m in range(0, 60, step)]


def interval_options():
    return [
        discord.SelectOption(label="20分", value="20"),
        discord.SelectOption(label="25分", value="25"),
        discord.SelectOption(label="30分", value="30"),
    ]


def pick_defaults(options: list[discord.SelectOption], current_value: str | None):
    # discord.py の Select は option.default=True が効く
    for o in options:
        o.default = (current_value is not None and o.value == current_value)
    return options


# ========= embed builders =========
def build_setup_embed(st: dict) -> discord.Embed:
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)

    step = int(st.get("step", 1))
    day_key = st.get("day_key", "today")

    e.add_field(name="Step", value=str(step), inline=True)
    e.add_field(name="日付", value=fmt_day_label(day_key), inline=True)

    # Step1 input
    sh = st.get("start_h")
    sm = st.get("start_m")
    eh = st.get("end_h")
    em = st.get("end_m")

    start_txt = "未選択" if sh is None or sm is None else f"{int(sh):02d}:{int(sm):02d}"
    end_txt = "未選択" if eh is None or em is None else f"{int(eh):02d}:{int(em):02d}"

    e.add_field(name="開始", value=start_txt, inline=True)
    e.add_field(name="終了", value=end_txt, inline=True)

    if step >= 2:
        interval = st.get("interval_minutes")
        title = st.get("title") or "無題"
        notify = st.get("notify_channel_id")
        everyone = bool(st.get("mention_everyone", False))

        e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)
        e.add_field(name="タイトル", value=title, inline=False)
        e.add_field(name="通知チャンネル", value=(f"<#{notify}>" if notify else "このチャンネル"), inline=False)
        e.add_field(name="@everyone", value=("ON" if everyone else "OFF"), inline=True)

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e


# ========= Modal =========
class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=80, required=False)

    def __init__(self, st: dict):
        super().__init__(timeout=300)
        self.st = st

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        # 押したら「反映」して見えるように embed 更新
        try:
            await interaction.response.send_message("✅ タイトルを反映したよ", ephemeral=True)
        except Exception:
            pass


# ========= Setup Wizard View =========
class SetupWizardView(discord.ui.View):
    def __init__(self, st: dict):
        super().__init__(timeout=600)
        self.st = st
        step = int(st.get("step", 1))

        if step == 1:
            self._build_step1()
        else:
            self._build_step2()

    def _build_step1(self):
        day_key = self.st.get("day_key", "today")

        # Row0: day + next
        self.add_item(
            discord.ui.Button(
                label="今日",
                custom_id="setup:day:today",
                style=discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary,
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="明日",
                custom_id="setup:day:tomorrow",
                style=discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary,
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="次へ",
                custom_id="setup:step:2",
                style=discord.ButtonStyle.success,
                row=0,
            )
        )

        # defaults
        sh = None if self.st.get("start_h") is None else str(self.st["start_h"])
        sm = None if self.st.get("start_m") is None else str(self.st["start_m"])
        eh = None if self.st.get("end_h") is None else str(self.st["end_h"])
        em = None if self.st.get("end_m") is None else str(self.st["end_m"])

        # Row1-2: start
        self.add_item(
            discord.ui.Select(
                custom_id="setup:start_h",
                placeholder="開始(時)",
                min_values=1,
                max_values=1,
                options=pick_defaults(hour_options_start(), sh),
                row=1,
            )
        )
        self.add_item(
            discord.ui.Select(
                custom_id="setup:start_m",
                placeholder="開始(分)",
                min_values=1,
                max_values=1,
                options=pick_defaults(minute_options(5), sm),
                row=2,
            )
        )

        # Row3-4: end (end hour allow 24)
        self.add_item(
            discord.ui.Select(
                custom_id="setup:end_h",
                placeholder="終了(時)",
                min_values=1,
                max_values=1,
                options=pick_defaults(hour_options_end(), eh),
                row=3,
            )
        )
        self.add_item(
            discord.ui.Select(
                custom_id="setup:end_m",
                placeholder="終了(分)",
                min_values=1,
                max_values=1,
                options=pick_defaults(minute_options(5), em),
                row=4,
            )
        )

    def _build_step2(self):
        # Row0: interval
        cur_interval = None if self.st.get("interval_minutes") is None else str(self.st["interval_minutes"])
        self.add_item(
            discord.ui.Select(
                custom_id="setup:interval",
                placeholder="間隔（20/25/30）",
                min_values=1,
                max_values=1,
                options=pick_defaults(interval_options(), cur_interval),
                row=0,
            )
        )

        # Row1: title + everyone
        self.add_item(
            discord.ui.Button(
                label="📝 タイトル入力",
                custom_id="setup:title",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
        )
        everyone = bool(self.st.get("mention_everyone", False))
        self.add_item(
            discord.ui.Button(
                label="@everyone ON" if everyone else "@everyone OFF",
                custom_id="setup:everyone",
                style=discord.ButtonStyle.danger if everyone else discord.ButtonStyle.secondary,
                row=1,
            )
        )

        # Row2: notify channel select
        cs = discord.ui.ChannelSelect(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（未選択=このチャンネル）",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            row=2,
        )
        self.add_item(cs)

        # Row3: back + create
        self.add_item(
            discord.ui.Button(
                label="戻る",
                custom_id="setup:step:1",
                style=discord.ButtonStyle.secondary,
                row=3,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="作成",
                custom_id="setup:create",
                style=discord.ButtonStyle.success,
                row=3,
            )
        )


async def rerender_setup(interaction: discord.Interaction, st: dict):
    # 入力を「見える化」するために、毎回 embed+view を作り直して edit する
    step = int(st.get("step", 1))
    await interaction.response.edit_message(
        embed=build_setup_embed(st),
        view=SetupWizardView(st),
    )


# ========= /setup =========
@tree.command(name="setup", description="募集パネル作成ウィザードを開く")
async def setup(interaction: discord.Interaction):
    st = {
        "step": 1,
        "day_key": "today",  # 初期は今日
        "start_h": None,
        "start_m": None,
        "end_h": None,
        "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
    }
    draft[dkey(interaction)] = st
    await interaction.response.send_message(
        embed=build_setup_embed(st),
        view=SetupWizardView(st),
        ephemeral=False,
    )


# ========= slots panel rendering =========
def slot_status_emoji(row: dict, me: str) -> str:
    if row.get("is_break"):
        return "⚪"
    rb = row.get("reserved_by")
    if not rb:
        return "🟢"
    return "🔴"


def panel_embed(title: str, day_key: str, interval: int, slots: list[dict], me_user_id: str) -> discord.Embed:
    e = discord.Embed(title="募集パネル", color=0x2B2D31)
    e.add_field(name="📅", value=f"{fmt_day_label(day_key)}（JST） / interval {interval}min", inline=False)

    lines = []
    for r in slots:
        t = fmt_hm(datetime.fromisoformat(str(r["start_at"]).replace("Z", "+00:00")))
        emj = slot_status_emoji(r, me_user_id)
        if r.get("is_break"):
            lines.append(f"{emj} {t} 休憩")
        else:
            rb = r.get("reserved_by")
            if rb:
                # reserved_by は text、<@id> 形式にする
                lines.append(f"{emj} {t} <@{rb}>")
            else:
                lines.append(f"{emj} {t}")
    if not lines:
        lines = ["（枠なし）"]

    e.add_field(name="枠", value="\n".join(lines), inline=False)
    e.add_field(
        name="凡例",
        value="🟢空き / 🔴予約済み（本人は押すとキャンセル） / ⚪休憩（予約不可）",
        inline=False,
    )
    return e


class SlotButton(discord.ui.Button):
    def __init__(self, slot_id: int, label: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style, custom_id=f"slot:{slot_id}")
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        # 取得
        res = await db_to_thread(lambda: db_get_slot(self.slot_id))
        if not res.data:
            await interaction.followup.send("❌ 枠が見つからない", ephemeral=True)
            return
        slot = res.data[0]

        if slot.get("is_break"):
            await interaction.followup.send("❌ 休憩枠は予約できない", ephemeral=True)
            return

        current = slot.get("reserved_by")
        if current and current != user_id:
            await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
            return

        # トグル（本人ならキャンセル）
        if current == user_id:
            patch = {
                "reserved_by": None,
                "reserver_user_id": None,
                "reserver_name": None,
                "reserved_at": None,
            }
            await db_to_thread(lambda: db_update_slot(self.slot_id, patch))
            await interaction.followup.send("✅ キャンセルしたよ！", ephemeral=True)
        else:
            patch = {
                "reserved_by": user_id,
                "reserver_user_id": int(user_id),
                "reserver_name": interaction.user.display_name,
                "reserved_at": datetime.now(UTC).isoformat(),
            }
            await db_to_thread(lambda: db_update_slot(self.slot_id, patch))
            await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

        # パネル再描画（色と一覧反映）
        panel_id = slot["panel_id"]
        sres = await db_to_thread(lambda: db_list_slots(panel_id))
        slots = sres.data or []

        # panelsも引いてタイトル等に使う
        pres = await db_to_thread(lambda: sb.table("panels").select("*").eq("id", panel_id).limit(1).execute())
        p = pres.data[0] if pres.data else {}
        title = p.get("title", "募集パネル")
        day_key = p.get("day_key", "today")
        interval = int(p.get("interval_minutes", 30))

        new_embed = panel_embed(title, day_key, interval, slots, user_id)
        new_view = SlotsView(slots)

        try:
            await interaction.message.edit(embed=new_embed, view=new_view)
        except Exception:
            pass


class SlotsView(discord.ui.View):
    def __init__(self, slots: list[dict]):
        super().__init__(timeout=None)
        # Discordは1メッセージあたりボタン最大25
        for r in slots[:25]:
            t = fmt_hm(datetime.fromisoformat(str(r["start_at"]).replace("Z", "+00:00")))
            if r.get("is_break"):
                style = discord.ButtonStyle.secondary
            elif r.get("reserved_by"):
                style = discord.ButtonStyle.danger
            else:
                style = discord.ButtonStyle.success
            self.add_item(SlotButton(int(r["id"]), t, style))


# ========= /generate =========
@tree.command(name="generate", description="設定した内容で枠ボタンを生成して投稿")
async def generate(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    key = dkey(interaction)
    st = draft.get(key)
    if not st:
        await interaction.followup.send("❌ 先に /setup を開いてね", ephemeral=True)
        return

    # 必須チェック（開始/終了/間隔）
    if st.get("start_h") is None or st.get("start_m") is None or st.get("end_h") is None or st.get("end_m") is None:
        await interaction.followup.send("❌ 開始/終了が未選択。/setup からやり直してね", ephemeral=True)
        return
    if st.get("interval_minutes") is None:
        await interaction.followup.send("❌ 間隔が未選択。/setup で選んでね", ephemeral=True)
        return

    day_key = st.get("day_key", "today")
    title = st.get("title") or "無題"
    interval = int(st["interval_minutes"])
    mention_everyone = bool(st.get("mention_everyone", False))
    notify_channel_id = st.get("notify_channel_id") or str(interaction.channel_id)

    sh, sm = int(st["start_h"]), int(st["start_m"])
    eh, em = int(st["end_h"]), int(st["end_m"])

    # 24:00対応（終了だけ許す）
    if eh == 24 and em != 0:
        await interaction.followup.send("❌ 24時は 24:00 のみ対応です", ephemeral=True)
        return

    start_dt = build_dt(day_key, sh, sm)

    # end_dt 作成：24:00なら翌日0:00扱い
    if eh == 24:
        end_dt = build_dt(day_key, 0, 0) + timedelta(days=1)
    else:
        end_dt = build_dt(day_key, eh, em)

    # 日跨ぎ（例: 23→01）
    if end_dt <= start_dt:
        end_dt = end_dt + timedelta(days=1)

    # panels upsert
    row = {
        "guild_id": str(interaction.guild_id),
        "channel_id": str(interaction.channel_id),
        "day_key": day_key,
        "title": title,
        "start_at": start_dt.astimezone(UTC).isoformat(),
        "end_at": end_dt.astimezone(UTC).isoformat(),
        "interval_minutes": interval,
        "notify_channel_id": str(notify_channel_id),
        "mention_everyone": mention_everyone,
        "created_by": str(interaction.user.id),
        "created_at": datetime.now(UTC).isoformat(),
    }

    try:
        up = await db_to_thread(lambda: db_upsert_panel(row))
    except Exception as e:
        await interaction.followup.send(f"❌ panels 保存失敗: {e}", ephemeral=True)
        return

    # upsert後に panel_id を確実に得る（返却が不安定なことがあるので再取得）
    pres = await db_to_thread(lambda: db_get_panel(str(interaction.guild_id), day_key))
    if not pres.data:
        await interaction.followup.send("❌ panels が取得できない（保存はできたかも）", ephemeral=True)
        return
    panel = pres.data[0]
    panel_id = int(panel["id"])

    # slots 再生成：まず消す（重複回避）
    try:
        await db_to_thread(lambda: db_delete_slots(panel_id))
    except Exception:
        # 失敗しても続行（insert側で衝突するならDB側制約）
        pass

    # slots 作成
    slot_rows = []
    cur = start_dt
    while cur < end_dt:
        nxt = cur + timedelta(minutes=interval)
        slot_rows.append(
            {
                "panel_id": panel_id,
                "start_at": cur.astimezone(UTC).isoformat(),
                "end_at": nxt.astimezone(UTC).isoformat(),
                "slot_time": cur.astimezone(JST).strftime("%H:%M"),  # NOT NULL対策
                "is_break": False,
                "reserved_by": None,
                "reserver_user_id": None,
                "reserver_name": None,
                "reserved_at": None,
                "notified": False,
            }
        )
        cur = nxt

    try:
        ins = await db_to_thread(lambda: db_insert_slots(slot_rows))
    except Exception as e:
        await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)
        return

    # 投稿
    sres = await db_to_thread(lambda: db_list_slots(panel_id))
    slots = sres.data or []

    ch = interaction.guild.get_channel(int(notify_channel_id)) or interaction.channel

    if mention_everyone:
        try:
            await ch.send("@everyone 募集を開始しました！")
        except Exception:
            pass

    msg = await ch.send(
        embed=panel_embed(title, day_key, interval, slots, str(interaction.user.id)),
        view=SlotsView(slots),
    )

    # message_id 保存（失敗してもOK）
    try:
        await db_to_thread(lambda: db_update_panel(panel_id, {"panel_message_id": str(msg.id)}))
    except Exception:
        pass

    await interaction.followup.send("✅ 枠ボタンを生成して投稿した！", ephemeral=True)


# ========= component handling for setup wizard =========
@client.event
async def on_interaction(interaction: discord.Interaction):
    # スラッシュはdiscord.pyに任せる（429/ack事故を減らす）
    if interaction.type == discord.InteractionType.application_command:
        return

    if interaction.type != discord.InteractionType.component:
        return

    data = interaction.data or {}
    cid = data.get("custom_id") or ""
    if not cid.startswith("setup:"):
        return

    key = dkey(interaction)
    st = draft.get(key)
    if not st:
        # draft無い場合は何もしない（古いパネル等）
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 先に /setup を開いてね", ephemeral=True)
        except Exception:
            pass
        return

    # セレクト値
    values = data.get("values") or []

    # --- day ---
    if cid == "setup:day:today":
        st["day_key"] = "today"
        if not interaction.response.is_done():
            await rerender_setup(interaction, st)
        return
    if cid == "setup:day:tomorrow":
        st["day_key"] = "tomorrow"
        if not interaction.response.is_done():
            await rerender_setup(interaction, st)
        return

    # --- step move ---
    if cid == "setup:step:2":
        st["step"] = 2
        if not interaction.response.is_done():
            await rerender_setup(interaction, st)
        return
    if cid == "setup:step:1":
        st["step"] = 1
        if not interaction.response.is_done():
            await rerender_setup(interaction, st)
        return

    # --- selects (反映して即見せる) ---
    if cid == "setup:start_h" and values:
        st["start_h"] = int(values[0])
        await rerender_setup(interaction, st)
        return
    if cid == "setup:start_m" and values:
        st["start_m"] = int(values[0])
        await rerender_setup(interaction, st)
        return
    if cid == "setup:end_h" and values:
        st["end_h"] = int(values[0])
        await rerender_setup(interaction, st)
        return
    if cid == "setup:end_m" and values:
        st["end_m"] = int(values[0])
        await rerender_setup(interaction, st)
        return
    if cid == "setup:interval" and values:
        st["interval_minutes"] = int(values[0])
        await rerender_setup(interaction, st)
        return
    if cid == "setup:notify_channel" and values:
        # valuesはチャンネルID文字列
        st["notify_channel_id"] = str(values[0])
        await rerender_setup(interaction, st)
        return

    # --- buttons ---
    if cid == "setup:everyone":
        st["mention_everyone"] = not bool(st.get("mention_everyone", False))
        await rerender_setup(interaction, st)
        return

    if cid == "setup:title":
        # Modalは別応答になるので、ここではdeferしてから出す
        await interaction.response.send_modal(TitleModal(st))
        return

    if cid == "setup:create":
        # createは保存だけして次にgenerate促す（Step2で押す想定）
        # 保存時点で不足があればエラー
        if st.get("start_h") is None or st.get("start_m") is None or st.get("end_h") is None or st.get("end_m") is None:
            await interaction.response.send_message("❌ 開始/終了が未選択。Step1で選んでね", ephemeral=True)
            return
        if st.get("interval_minutes") is None:
            await interaction.response.send_message("❌ 間隔が未選択。Step2で選んでね", ephemeral=True)
            return

        # ここでは panels に保存だけ（実生成は /generate）
        day_key = st.get("day_key", "today")
        sh, sm = int(st["start_h"]), int(st["start_m"])
        eh, em = int(st["end_h"]), int(st["end_m"])

        if eh == 24 and em != 0:
            await interaction.response.send_message("❌ 24時は 24:00 のみ対応です", ephemeral=True)
            return

        start_dt = build_dt(day_key, sh, sm)
        if eh == 24:
            end_dt = build_dt(day_key, 0, 0) + timedelta(days=1)
        else:
            end_dt = build_dt(day_key, eh, em)
        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)

        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),
            "day_key": day_key,
            "title": st.get("title") or "無題",
            "start_at": start_dt.astimezone(UTC).isoformat(),
            "end_at": end_dt.astimezone(UTC).isoformat(),
            "interval_minutes": int(st["interval_minutes"]),
            "notify_channel_id": str(st.get("notify_channel_id") or interaction.channel_id),
            "mention_everyone": bool(st.get("mention_everyone", False)),
            "created_by": str(interaction.user.id),
            "created_at": datetime.now(UTC).isoformat(),
        }

        await interaction.response.defer(ephemeral=True)
        try:
            await db_to_thread(lambda: db_upsert_panel(row))
        except Exception as e:
            await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
            return

        await interaction.followup.send("✅ 保存できた！次は /generate で枠ボタン生成してね", ephemeral=True)
        return

    # ここまでで拾ってないcustom_idはACKだけして落とす（無反応防止）
    if not interaction.response.is_done():
        await interaction.response.defer()
    return


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


async def main():
    # 429避け（起動直後に連打しない）
    await asyncio.sleep(3)
    await client.start(TOKEN)


asyncio.run(main())