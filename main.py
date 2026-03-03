import os
import asyncio
from datetime import datetime, timedelta, timezone, date

import discord
from discord import app_commands
from supabase import create_client

# =========================================================
# ENV
# =========================================================
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

# =========================================================
# DISCORD
# =========================================================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================================================
# STATE (wizard draft)
# key: (guild_id, user_id) -> dict
# =========================================================
draft: dict[tuple[str, str], dict] = {}


def dkey(interaction: discord.Interaction) -> tuple[str, str]:
    return (str(interaction.guild_id), str(interaction.user.id))


async def db_to_thread(fn):
    return await asyncio.to_thread(fn)


# =========================================================
# DB helpers
# =========================================================
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


def delete_slots_by_panel(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()


def insert_slots(rows: list[dict]):
    return sb.table("slots").insert(rows).execute()


def get_slots(panel_id: int):
    return sb.table("slots").select("*").eq("panel_id", panel_id).order("start_at").execute()


def clear_slot_reserved(slot_id: int):
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


def set_slot_reserved(slot_id: int, user_id: str, user_name: str):
    return (
        sb.table("slots")
        .update(
            {
                "reserved_by": user_id,
                "reserver_user_id": int(user_id),
                "reserver_name": user_name,
                "reserved_at": datetime.now(UTC).isoformat(),
            }
        )
        .eq("id", slot_id)
        .is_("reserved_by", "null")
        .execute()
    )


# =========================================================
# UI options/helpers
# =========================================================
def _cur(base: str, v):
    # 7枚目風：placeholderに「現在:xx」を表示
    return f"{base} 現在:{v:02d}" if isinstance(v, int) else f"{base} 現在:--"


def hour_options_0_23():
    return [discord.SelectOption(label=f"{h:02d}", value=str(h)) for h in range(24)]


def hour_options_0_24_for_end():
    return [discord.SelectOption(label=f"{h:02d}", value=str(h)) for h in range(25)]  # 24含む


def minute_options(step=5):
    return [discord.SelectOption(label=f"{m:02d}", value=str(m)) for m in range(0, 60, step)]


def interval_options():
    return [
        discord.SelectOption(label="20分", value="20"),
        discord.SelectOption(label="25分", value="25"),
        discord.SelectOption(label="30分", value="30"),
    ]


def hm(st: dict, prefix: str) -> str | None:
    h = st.get(f"{prefix}_h")
    m = st.get(f"{prefix}_m")
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"


def day_label(day_key: str) -> str:
    return "今日" if day_key == "today" else "明日"


def build_setup_embed(st: dict) -> discord.Embed:
    step = int(st.get("step", 1))
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)

    e.add_field(name="Step", value=str(step), inline=False)
    e.add_field(name="日付", value=day_label(st["day_key"]), inline=True)

    start = hm(st, "start")
    end = hm(st, "end")
    e.add_field(name="開始", value=(start or "未選択"), inline=True)
    e.add_field(name="終了", value=(end or "未選択"), inline=True)

    if step >= 2:
        interval = st.get("interval_minutes")
        e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)

        title = st.get("title") or "無題"
        e.add_field(name="タイトル", value=title, inline=False)

        notify_id = st.get("notify_channel_id")
        notify_txt = f"<#{notify_id}>" if notify_id else "このチャンネル（未選択）"
        e.add_field(name="通知チャンネル（3分前通知用）", value=notify_txt, inline=False)

        everyone = bool(st.get("mention_everyone", False))
        e.add_field(name="@everyone", value=("ON" if everyone else "OFF"), inline=True)

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e


# =========================================================
# TIME helpers for slots
# =========================================================
def jst_today() -> date:
    return datetime.now(JST).date()


def panel_base_day(day_key: str) -> date:
    d = jst_today()
    return d if day_key == "today" else (d + timedelta(days=1))


def make_dt_jst(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=JST)


# =========================================================
# MODAL
# =========================================================
class TitleModal(discord.ui.Modal, title="タイトル入力"):
    title_in = discord.ui.TextInput(
        label="タイトル",
        placeholder="例：今日の部屋管理",
        max_length=50,
        required=False,
    )

    def __init__(self, key: tuple[str, str]):
        super().__init__(timeout=300)
        self.key = key

    async def on_submit(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            await interaction.response.send_message("❌ 先に /setup からやり直してね", ephemeral=True)
            return

        st["title"] = (self.title_in.value or "").strip() or "無題"

        # モーダルの元メッセージを更新
        view = SetupWizardView(self.key)
        embed = build_setup_embed(st)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send("✅ タイトルを反映したよ", ephemeral=True)


# =========================================================
# SETUP WIZARD VIEW (7枚目風: 現在値表示)
# =========================================================
class SetupWizardView(discord.ui.View):
    def __init__(self, key: tuple[str, str]):
        super().__init__(timeout=600)
        self.key = key

        st = draft.get(key)
        if not st:
            return

        step = int(st.get("step", 1))
        if step == 1:
            self._build_step1(st)
        else:
            self._build_step2(st)

    def _build_step1(self, st: dict):
        # 今日/明日（選択中を強調）
        today_style = discord.ButtonStyle.primary if st["day_key"] == "today" else discord.ButtonStyle.secondary
        tomo_style = discord.ButtonStyle.primary if st["day_key"] == "tomorrow" else discord.ButtonStyle.secondary
        self.add_item(DayButton(self.key, "today", "今日", today_style))
        self.add_item(DayButton(self.key, "tomorrow", "明日", tomo_style))
        self.add_item(NextButton(self.key))

        # 7枚目風：placeholderに現在値
        self.add_item(TimeSelect(self.key, "start_h", _cur("開始(時)", st.get("start_h")), hour_options_0_23(), row=1))
        self.add_item(TimeSelect(self.key, "start_m", _cur("開始(分)", st.get("start_m")), minute_options(5), row=2))
        self.add_item(TimeSelect(self.key, "end_h", _cur("終了(時)", st.get("end_h")), hour_options_0_24_for_end(), row=3))
        self.add_item(TimeSelect(self.key, "end_m", _cur("終了(分)", st.get("end_m")), minute_options(5), row=4))

    def _build_step2(self, st: dict):
        # 間隔（現在表示）
        self.add_item(IntervalSelect(self.key, _cur("間隔（分）", st.get("interval_minutes")), row=1))

        # タイトル
        self.add_item(TitleButton(self.key, row=2))

        # everyone（ON=緑 / OFF=灰、ラベルも変える）
        on = bool(st.get("mention_everyone", False))
        style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary
        label = "@everyone ON" if on else "@everyone OFF"
        self.add_item(EveryoneToggleButton(self.key, label=label, style=style, row=2))

        # 通知チャンネル（3分前通知用）→ 選んだらembedで見える
        self.add_item(NotifyChannelSelect(self.key, row=3))

        # 戻る/作成
        self.add_item(BackButton(self.key, row=4))
        self.add_item(CreateButton(self.key, row=4))


class DayButton(discord.ui.Button):
    def __init__(self, key, day_key, label, style):
        super().__init__(label=label, style=style, custom_id=f"setup:day:{day_key}", row=0)
        self.key = key
        self.day_key = day_key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        st["day_key"] = self.day_key
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupWizardView(self.key))


class TimeSelect(discord.ui.Select):
    def __init__(self, key, field, placeholder, options, row):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"setup:{field}",
            row=row,
        )
        self.key = key
        self.field = field

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        st[self.field] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupWizardView(self.key))


class IntervalSelect(discord.ui.Select):
    def __init__(self, key, placeholder, row):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=interval_options(),
            custom_id="setup:interval",
            row=row,
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        st["interval_minutes"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupWizardView(self.key))


class NextButton(discord.ui.Button):
    def __init__(self, key):
        super().__init__(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=0)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        if hm(st, "start") is None or hm(st, "end") is None:
            return await interaction.response.send_message("❌ 開始/終了（時・分）を選んでね", ephemeral=True)

        st["step"] = 2
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupWizardView(self.key))


class BackButton(discord.ui.Button):
    def __init__(self, key, row):
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        st["step"] = 1
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupWizardView(self.key))


class TitleButton(discord.ui.Button):
    def __init__(self, key, row):
        super().__init__(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
        await interaction.response.send_modal(TitleModal(self.key))


class EveryoneToggleButton(discord.ui.Button):
    def __init__(self, key, label, style, row):
        super().__init__(label=label, style=style, custom_id="setup:everyone", row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        st["mention_everyone"] = not bool(st.get("mention_everyone", False))
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupWizardView(self.key))


class NotifyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, key, row):
        super().__init__(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（3分前通知用）",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            row=row,
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        ch = self.values[0]
        st["notify_channel_id"] = str(ch.id)
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupWizardView(self.key))


# =========================================================
# PUBLIC PANEL (slots)
# =========================================================
def build_panel_embed(panel: dict, slots: list[dict]) -> discord.Embed:
    title = panel.get("title", "募集パネル")
    interval = int(panel.get("interval_minutes", 30))
    day_key = panel.get("day_key", "today")

    e = discord.Embed(title=title, color=0x2B2D31)
    e.description = f"📅 {day_label(day_key)}（JST） / interval {interval}min"

    lines = []
    for r in slots[:12]:
        t = r.get("slot_time", "??:??")
        if r.get("is_break"):
            lines.append(f"⚪ {t} 休憩")
        elif r.get("reserved_by"):
            lines.append(f"🔴 {t} <@{r['reserved_by']}>")
        else:
            lines.append(f"🟢 {t}")

    e.add_field(name="枠", value="\n".join(lines) if lines else "（なし）", inline=False)
    e.add_field(name="凡例", value="🟢空き / 🔴予約済み（本人は押すとキャンセル） / ⚪休憩（予約不可）", inline=False)
    return e


class SlotsView(discord.ui.View):
    def __init__(self, panel_id: int, slots: list[dict]):
        super().__init__(timeout=None)
        self.panel_id = panel_id

        for r in slots[:20]:
            sid = int(r["id"])
            label = r.get("slot_time", "??:??")
            is_break = bool(r.get("is_break", False))
            reserved_by = r.get("reserved_by")

            if is_break:
                style = discord.ButtonStyle.secondary
                disabled = True
            elif reserved_by:
                style = discord.ButtonStyle.danger
                disabled = False
            else:
                style = discord.ButtonStyle.success
                disabled = False

            self.add_item(SlotButton(panel_id, sid, label, style, disabled))


class SlotButton(discord.ui.Button):
    def __init__(self, panel_id: int, slot_id: int, label: str, style: discord.ButtonStyle, disabled: bool):
        super().__init__(label=label, style=style, custom_id=f"slot:{slot_id}", disabled=disabled)
        self.panel_id = panel_id
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name

        # 最新読み込み
        def work_get():
            return sb.table("slots").select("*").eq("id", self.slot_id).limit(1).execute()

        res = await db_to_thread(work_get)
        if not res.data:
            return await interaction.followup.send("❌ 枠が見つからない", ephemeral=True)

        slot = res.data[0]
        if bool(slot.get("is_break", False)):
            return await interaction.followup.send("❌ 休憩枠です", ephemeral=True)

        reserved_by = slot.get("reserved_by")

        if reserved_by and str(reserved_by) == user_id:
            # キャンセル
            await db_to_thread(lambda: clear_slot_reserved(self.slot_id))
            await interaction.followup.send("✅ キャンセルしたよ", ephemeral=True)
        else:
            if reserved_by:
                return await interaction.followup.send("❌ すでに予約されています", ephemeral=True)

            up = await db_to_thread(lambda: set_slot_reserved(self.slot_id, user_id, user_name))
            if not up.data:
                return await interaction.followup.send("❌ 先に取られた", ephemeral=True)
            await interaction.followup.send("✅ 予約したよ", ephemeral=True)

        # パネル更新
        await refresh_panel(interaction.guild, self.panel_id)


async def refresh_panel(guild: discord.Guild, panel_id: int):
    # panel
    pres = await db_to_thread(lambda: sb.table("panels").select("*").eq("id", panel_id).limit(1).execute())
    if not pres.data:
        return
    panel = pres.data[0]
    msg_id = panel.get("panel_message_id")
    ch_id = panel.get("channel_id")
    if not msg_id or not ch_id:
        return

    sres = await db_to_thread(lambda: get_slots(panel_id))
    slots = sres.data or []

    ch = guild.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return

    try:
        msg = await ch.fetch_message(int(msg_id))
    except Exception:
        return

    await msg.edit(embed=build_panel_embed(panel, slots), view=SlotsView(panel_id, slots))


# =========================================================
# CREATE (Step2 "作成") -> 公開投稿まで一気にやる
# =========================================================
class CreateButton(discord.ui.Button):
    def __init__(self, key, row):
        super().__init__(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(self.key)
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        start_hm = hm(st, "start")
        end_hm = hm(st, "end")
        interval = st.get("interval_minutes")
        if not start_hm or not end_hm or not interval:
            return await interaction.response.send_message("❌ 未選択があります（開始/終了/間隔）", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        gid = str(interaction.guild_id)
        uid = str(interaction.user.id)
        day_key = st["day_key"]

        # panels 保存（DB列に合わせる）
        row = {
            "guild_id": gid,
            "channel_id": str(interaction.channel_id),   # パネル投稿先（公開）
            "day_key": day_key,
            "title": st.get("title") or "無題",
            "interval_minutes": int(interval),
            "notify_channel_id": st.get("notify_channel_id"),  # 3分前通知用
            "mention_everyone": bool(st.get("mention_everyone", False)),
            "created_by": uid,
            "created_at": datetime.now(UTC).isoformat(),
            "start_h": int(st["start_h"]),
            "start_m": int(st["start_m"]),
            "end_h": int(st["end_h"]),
            "end_m": int(st["end_m"]),
            "start_hm": start_hm,
            "end_hm": end_hm,
        }

        try:
            await db_to_thread(lambda: upsert_panel(row))
        except Exception as e:
            return await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)

        pres = await db_to_thread(lambda: get_panel(gid, day_key))
        if not pres.data:
            return await interaction.followup.send("❌ panels が取れない", ephemeral=True)

        panel = pres.data[0]
        panel_id = int(panel["id"])

        # slots 生成（既存削除）
        try:
            await db_to_thread(lambda: delete_slots_by_panel(panel_id))
        except Exception:
            pass

        base = panel_base_day(day_key)

        sh = int(panel["start_h"])
        sm = int(panel["start_m"])
        eh = int(panel["end_h"])
        em = int(panel["end_m"])
        interval = int(panel["interval_minutes"])

        start_dt = make_dt_jst(base, sh, sm)

        # 24:00対応
        if eh == 24 and em == 0:
            end_dt = make_dt_jst(base, 0, 0) + timedelta(days=1)
        else:
            end_dt = make_dt_jst(base, eh, em)

        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)

        rows = []
        cur = start_dt
        while cur < end_dt:
            nxt = cur + timedelta(minutes=interval)
            rows.append(
                {
                    "panel_id": panel_id,
                    "start_at": cur.astimezone(UTC).isoformat(),
                    "end_at": nxt.astimezone(UTC).isoformat(),
                    "slot_time": cur.strftime("%H:%M"),
                    "is_break": False,
                    "notified": False,
                    "reserved_by": None,
                    "reserver_user_id": None,
                    "reserver_name": None,
                    "reserved_at": None,
                }
            )
            cur = nxt

        ins = await db_to_thread(lambda: insert_slots(rows))
        slots = ins.data or []
        if not slots:
            return await interaction.followup.send("❌ slots が作れなかった（slotsの列/NOT NULLを確認）", ephemeral=True)

        # 公開投稿
        content = "@everyone 募集を開始しました！" if bool(panel.get("mention_everyone", False)) else ""
        msg = await interaction.channel.send(content=content, embed=build_panel_embed(panel, slots), view=SlotsView(panel_id, slots))

        # message_id保存
        try:
            await db_to_thread(lambda: update_panel_message_id(panel_id, str(msg.id)))
        except Exception:
            pass

        # ドラフト消す
        draft.pop(self.key, None)

        await interaction.followup.send("✅ 募集パネルを投稿した！", ephemeral=True)


# =========================================================
# COMMAND
# =========================================================
@tree.command(name="setup", description="募集パネルを作る（準備画面は自分だけ）")
async def setup_cmd(interaction: discord.Interaction):
    key = dkey(interaction)

    draft[key] = {
        "step": 1,
        "day_key": "today",      # 初期は今日
        "start_h": None,
        "start_m": None,
        "end_h": None,           # 24まで許可
        "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,  # 3分前通知用（未選択=このチャンネル）
    }

    st = draft[key]
    await interaction.response.send_message(
        "設定して「作成」してね👇（この画面は自分だけ見える）",
        embed=build_setup_embed(st),
        view=SetupWizardView(key),
        ephemeral=True,
    )


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


async def main():
    await asyncio.sleep(2)
    await client.start(TOKEN)


asyncio.run(main())