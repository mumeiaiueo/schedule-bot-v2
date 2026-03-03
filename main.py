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


# ---- 状態（作成中の一時保存）----
# key: (guild_id, user_id) -> dict
draft: dict[tuple[str, str], dict] = {}


def dkey(interaction: discord.Interaction) -> tuple[str, str]:
    return (str(interaction.guild_id), str(interaction.user.id))


async def db_to_thread(fn):
    return await asyncio.to_thread(fn)


# ========= DB helpers =========
def upsert_panel(row: dict):
    # panels に (guild_id, day_key) のユニーク制約がある前提
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


def get_slot(slot_id: int):
    return sb.table("slots").select("*").eq("id", slot_id).limit(1).execute()


def update_slot(slot_id: int, patch: dict):
    return sb.table("slots").update(patch).eq("id", slot_id).execute()


# ========= UI helpers =========
def hour_options():
    return [discord.SelectOption(label=f"{h:02d}", value=str(h)) for h in range(24)]


def minute_options(step=5):
    return [discord.SelectOption(label=f"{m:02d}", value=str(m)) for m in range(0, 60, step)]


def interval_options():
    return [
        discord.SelectOption(label="20分", value="20"),
        discord.SelectOption(label="25分", value="25"),
        discord.SelectOption(label="30分", value="30"),
    ]


def hm_from_state(st: dict, prefix: str) -> str | None:
    h = st.get(f"{prefix}_h")
    m = st.get(f"{prefix}_m")
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"


def day_label(day_key: str) -> str:
    return "今日" if day_key == "today" else "明日"


def build_setup_embed(st: dict) -> discord.Embed:
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)

    e.add_field(name="Step", value=str(st.get("step", 1)), inline=True)
    e.add_field(name="日付", value=day_label(st.get("day_key", "today")), inline=True)

    start = hm_from_state(st, "start")
    end = hm_from_state(st, "end")
    e.add_field(name="開始", value=(start or "未選択"), inline=True)
    e.add_field(name="終了", value=(end or "未選択"), inline=True)

    interval = st.get("interval_minutes")
    e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)

    title = st.get("title") or "無題"
    e.add_field(name="タイトル", value=title, inline=False)

    # ★重要：ここは「3分前通知の送信先」
    notify = st.get("notify_channel_id")
    e.add_field(
        name="通知チャンネル（3分前通知の送信先）",
        value=(f"<#{notify}>" if notify else "未選択＝このチャンネル"),
        inline=False,
    )

    everyone = bool(st.get("mention_everyone", False))
    e.add_field(name="@everyone", value=("ON" if everyone else "OFF"), inline=True)

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e


def jst_today() -> date:
    return datetime.now(JST).date()


def to_panel_day(day_key: str) -> date:
    d = jst_today()
    return d if day_key == "today" else (d + timedelta(days=1))


def make_dt_jst(d: date, hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=JST)


def slot_time_str(dt_jst: datetime) -> str:
    return dt_jst.strftime("%H:%M")


# ========= Modal =========
class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(
        label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False
    )

    def __init__(self, st: dict, msg: discord.Message):
        super().__init__(timeout=300)
        self.st = st
        self.msg = msg

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        # 入力内容を embed に即反映
        await interaction.response.edit_message(
            embed=build_setup_embed(self.st),
            view=SetupView(self.st, self.msg),
        )


# ========= View =========
class SetupView(discord.ui.View):
    """
    毎回 View を作り直して、
    - 選ばれてるボタンを強調
    - @everyone の色を切替
    - 選択内容を embed に反映
    を安定してやる
    """

    def __init__(self, st: dict, msg: discord.Message):
        super().__init__(timeout=None)
        self.st = st
        self.msg = msg

        step = int(st.get("step", 1))
        day_key = st.get("day_key", "today")
        everyone = bool(st.get("mention_everyone", False))

        # --- Row0: 今日/明日 + 次へ/戻る/作成
        self.add_item(DayButton("今日", "today", style=discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary, row=0))
        self.add_item(DayButton("明日", "tomorrow", style=discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary, row=0))

        if step == 1:
            self.add_item(NextButton(row=0))
        else:
            self.add_item(BackButton(row=0))
            self.add_item(CreateButton(row=0))

        # --- Step1: 時刻選択
        if step == 1:
            self.add_item(TimeSelect("setup:start_h", "開始(時)", hour_options(), row=1))
            self.add_item(TimeSelect("setup:start_m", "開始(分)", minute_options(5), row=2))
            self.add_item(TimeSelect("setup:end_h", "終了(時)", hour_options(), row=3))
            self.add_item(TimeSelect("setup:end_m", "終了(分)", minute_options(5), row=4))

        # --- Step2: 詳細設定
        if step == 2:
            self.add_item(IntervalSelect(row=1))
            self.add_item(TitleButton(row=2))
            # ONなら緑 / OFFなら赤
            self.add_item(EveryoneButton(style=discord.ButtonStyle.success if everyone else discord.ButtonStyle.danger, row=2))
            self.add_item(NotifyChannelSelect(row=3))


class DayButton(discord.ui.Button):
    def __init__(self, label: str, day_key: str, style: discord.ButtonStyle, row: int):
        super().__init__(label=label, style=style, custom_id=f"setup:day:{day_key}", row=row)
        self.day_key = day_key

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
        st["day_key"] = self.day_key
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))  # type: ignore


class NextButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        # Step1 の入力必須チェック
        if hm_from_state(st, "start") is None or hm_from_state(st, "end") is None:
            return await interaction.response.send_message("❌ 開始/終了（時・分）を選んでね", ephemeral=True)

        st["step"] = 2
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))  # type: ignore


class BackButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
        st["step"] = 1
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))  # type: ignore


class TimeSelect(discord.ui.Select):
    def __init__(self, cid: str, placeholder: str, options: list[discord.SelectOption], row: int):
        super().__init__(custom_id=cid, placeholder=placeholder, options=options, min_values=1, max_values=1, row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        v = int(self.values[0])
        if self.custom_id == "setup:start_h":
            st["start_h"] = v
        elif self.custom_id == "setup:start_m":
            st["start_m"] = v
        elif self.custom_id == "setup:end_h":
            st["end_h"] = v
        elif self.custom_id == "setup:end_m":
            st["end_m"] = v

        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))  # type: ignore


class IntervalSelect(discord.ui.Select):
    def __init__(self, row: int):
        super().__init__(
            custom_id="setup:interval_minutes",
            placeholder="間隔（20/25/30）",
            options=interval_options(),
            min_values=1,
            max_values=1,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
        st["interval_minutes"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))  # type: ignore


class TitleButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
        await interaction.response.send_modal(TitleModal(st, interaction.message))  # type: ignore


class EveryoneButton(discord.ui.Button):
    def __init__(self, style: discord.ButtonStyle, row: int):
        super().__init__(label="@everyone ON/OFF", style=style, custom_id="setup:everyone", row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
        st["mention_everyone"] = not bool(st.get("mention_everyone", False))
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))  # type: ignore


class NotifyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, row: int):
        super().__init__(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（3分前通知の送信先）",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        ch = self.values[0]
        st["notify_channel_id"] = str(ch.id)
        await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st, interaction.message))  # type: ignore


class CreateButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=row)

    async def callback(self, interaction: discord.Interaction):
        st = draft.get(dkey(interaction))
        if not st:
            return await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)

        # 必須チェック
        start_hm = hm_from_state(st, "start")
        end_hm = hm_from_state(st, "end")
        interval = st.get("interval_minutes")
        if not start_hm or not end_hm or not interval:
            return await interaction.response.send_message("❌ 開始/終了/間隔 が未選択。Stepを埋めてね", ephemeral=True)

        # panelsへ保存（※notify_channel_id は「3分前通知先」）
        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),  # パネルを出した場所（枠を置く場所）
            "day_key": st.get("day_key", "today"),
            "title": st.get("title", "無題"),
            "interval_minutes": int(interval),
            "notify_channel_id": st.get("notify_channel_id"),  # 3分前通知先
            "mention_everyone": bool(st.get("mention_everyone", False)),
            "created_by": str(interaction.user.id),
            "created_at": datetime.now(timezone.utc).isoformat(),
            # DBにある想定の列（あなたの panels 画面にあった）
            "start_hm": start_hm,
            "end_hm": end_hm,
            "start_h": int(st["start_h"]),
            "start_m": int(st["start_m"]),
            "end_h": int(st["end_h"]),
            "end_m": int(st["end_m"]),
        }

        await interaction.response.defer(ephemeral=True)
        try:
            await db_to_thread(lambda: upsert_panel(row))
        except Exception as e:
            await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
            return

        # @everyone は「作成時1回だけ送る」
        if bool(st.get("mention_everyone", False)):
            try:
                await interaction.channel.send("@everyone 募集を開始しました！")  # type: ignore
            except Exception:
                pass

        await interaction.followup.send("✅ 保存できた！次は /generate で枠ボタン生成してね", ephemeral=True)


# ========= Slots UI =========
def build_panel_embed(panel: dict, slots: list[dict]) -> discord.Embed:
    # 表示イメージ：日付 / interval / 枠の一覧 + 凡例
    day_key = panel.get("day_key", "today")
    interval = int(panel.get("interval_minutes", 30))
    title = panel.get("title", "募集パネル")

    d = to_panel_day(day_key)
    header = f"{d.isoformat()} (JST)  / interval {interval}min"

    e = discord.Embed(title=title, description=header, color=0x2B2D31)

    lines = []
    for s in slots:
        t = s["slot_time"]
        is_break = bool(s.get("is_break", False))
        reserved_by = s.get("reserved_by")
        if is_break:
            lines.append(f"⚪ {t} 休憩")
        elif reserved_by:
            lines.append(f"🔴 {t} <@{reserved_by}>")
        else:
            lines.append(f"🟢 {t}")

    e.add_field(name="枠", value="\n".join(lines) if lines else "なし", inline=False)
    e.add_field(name="凡例", value="🟢空き / 🔴予約済み（本人は押すとキャンセル） / ⚪休憩（予約不可）", inline=False)
    return e


class SlotsView(discord.ui.View):
    def __init__(self, panel_id: int, slots: list[dict]):
        super().__init__(timeout=None)
        self.panel_id = panel_id

        # 25ボタン制限があるので最大20個まで
        for s in slots[:20]:
            self.add_item(SlotButton(slot_id=int(s["id"])))

        # ここに「通知ON」や「休憩切替」などを後で追加できる


class SlotButton(discord.ui.Button):
    def __init__(self, slot_id: int):
        super().__init__(label="...", style=discord.ButtonStyle.secondary, custom_id=f"slot:{slot_id}")
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 最新状態をDBから取得
        try:
            res = await db_to_thread(lambda: get_slot(self.slot_id))
        except Exception as e:
            return await interaction.followup.send(f"❌ 取得失敗: {e}", ephemeral=True)

        if not res.data:
            return await interaction.followup.send("❌ その枠が見つからない", ephemeral=True)

        s = res.data[0]
        if bool(s.get("is_break", False)):
            return await interaction.followup.send("❌ 休憩枠は予約できない", ephemeral=True)

        user_id = str(interaction.user.id)
        reserved_by = s.get("reserved_by")

        # 本人なら「押すとキャンセル」
        if reserved_by and str(reserved_by) == user_id:
            patch = {
                "reserved_by": None,
                "reserver_user_id": None,
                "reserver_name": None,
                "reserved_at": None,
            }
            try:
                await db_to_thread(lambda: update_slot(self.slot_id, patch))
            except Exception as e:
                return await interaction.followup.send(f"❌ キャンセル失敗: {e}", ephemeral=True)

            await interaction.followup.send("✅ キャンセルしたよ！", ephemeral=True)
        else:
            # 他人が既に予約済みなら不可
            if reserved_by:
                return await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)

            patch = {
                "reserved_by": user_id,
                "reserver_user_id": int(user_id),
                "reserver_name": interaction.user.display_name,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await db_to_thread(lambda: update_slot(self.slot_id, patch))
            except Exception as e:
                return await interaction.followup.send(f"❌ 予約失敗: {e}", ephemeral=True)

            await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

        # パネルを即更新（同じメッセージを編集）
        try:
            msg = interaction.message  # type: ignore
            # panel_id を slots から辿る
            panel_id = int(s["panel_id"])
            # panel と slots を再取得
            pres = await db_to_thread(lambda: sb.table("panels").select("*").eq("id", panel_id).limit(1).execute())
            sres = await db_to_thread(lambda: sb.table("slots").select("*").eq("panel_id", panel_id).order("start_at").execute())
            if pres.data and sres.data:
                panel = pres.data[0]
                slots = sres.data
                await msg.edit(embed=build_panel_embed(panel, slots), view=SlotsView(panel_id, slots))
        except Exception:
            pass


# ========= Commands =========
@tree.command(name="setup", description="募集パネルを作る（設定画面を出す）")
async def setup(interaction: discord.Interaction):
    key = dkey(interaction)

    # 初期値：今日がデフォ
    draft[key] = {
        "step": 1,
        "day_key": "today",
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,  # 3分前通知先（未選択=このチャンネル）
    }

    st = draft[key]
    await interaction.response.send_message(
        "ボタン/セレクトで設定してね👇",
        embed=build_setup_embed(st),
        view=SetupView(st, interaction.message if interaction.message else None),  # type: ignore
        ephemeral=False,
    )


@tree.command(name="generate", description="保存した設定から枠ボタンを生成して投稿")
async def generate(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id)

    # today 固定じゃなく、保存されてる day_key で作りたいならここを選択式にできる（今は today）
    day_key = "today"

    # panels 取得
    try:
        pres = await db_to_thread(lambda: get_panel(guild_id, day_key))
    except Exception as e:
        return await interaction.followup.send(f"❌ panels 取得失敗: {e}", ephemeral=True)

    if not pres.data:
        return await interaction.followup.send("❌ 先に /setup → 作成 をしてね", ephemeral=True)

    panel = pres.data[0]
    panel_id = int(panel["id"])

    start_hm = panel.get("start_hm")
    end_hm = panel.get("end_hm")
    interval = int(panel.get("interval_minutes", 30))

    if not start_hm or not end_hm:
        return await interaction.followup.send("❌ 開始/終了が保存されてない。/setup からやり直してね", ephemeral=True)

    d = to_panel_day(panel.get("day_key", "today"))
    start_dt = make_dt_jst(d, start_hm)
    end_dt = make_dt_jst(d, end_hm)

    # 日跨ぎ対応（例: 23:00 → 01:00）
    if end_dt <= start_dt:
        end_dt = end_dt + timedelta(days=1)

    # 既存slot削除（重複対策）
    try:
        await db_to_thread(lambda: delete_slots(panel_id))
    except Exception as e:
        return await interaction.followup.send(f"❌ 既存slots削除失敗: {e}", ephemeral=True)

    # slots 生成
    rows = []
    cur = start_dt
    while cur < end_dt:
        nxt = cur + timedelta(minutes=interval)
        rows.append({
            "panel_id": panel_id,
            "start_at": cur.astimezone(timezone.utc).isoformat(),
            "end_at": nxt.astimezone(timezone.utc).isoformat(),
            "slot_time": slot_time_str(cur),  # NOT NULL
            "is_break": False,               # NOT NULL
            "notified": False,               # NOT NULL
            "reserved_by": None,
            "reserver_user_id": None,
            "reserver_name": None,
            "reserved_at": None,
        })
        cur = nxt

    try:
        ins = await db_to_thread(lambda: insert_slots(rows))
    except Exception as e:
        return await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)

    slots = ins.data or []
    if not slots:
        return await interaction.followup.send("❌ slots が作れなかった（slots列/NOT NULL を確認）", ephemeral=True)

    # パネルを投稿する場所（＝setupを打ったチャンネル or いまのチャンネル）
    post_channel_id = panel.get("channel_id") or str(interaction.channel_id)
    ch = interaction.guild.get_channel(int(post_channel_id)) or interaction.channel  # type: ignore

    msg = await ch.send(embed=build_panel_embed(panel, slots), view=SlotsView(panel_id, slots))  # type: ignore

    # panels に panel_message_id 保存（あれば）
    try:
        await db_to_thread(lambda: sb.table("panels").update({"panel_message_id": str(msg.id)}).eq("id", panel_id).execute())
    except Exception:
        pass

    await interaction.followup.send("✅ 枠ボタンを生成して投稿した！", ephemeral=True)


@tree.command(name="reset", description="今日/明日の募集を削除（slotsも消す）")
@app_commands.describe(day="today か tomorrow")
async def reset(interaction: discord.Interaction, day: str = "today"):
    await interaction.response.defer(ephemeral=True)

    if day not in ("today", "tomorrow"):
        return await interaction.followup.send("❌ day は today / tomorrow のどっちか", ephemeral=True)

    guild_id = str(interaction.guild_id)
    try:
        pres = await db_to_thread(lambda: get_panel(guild_id, day))
    except Exception as e:
        return await interaction.followup.send(f"❌ panels取得失敗: {e}", ephemeral=True)

    if not pres.data:
        return await interaction.followup.send("✅ その日の panels は無いよ（何もしない）", ephemeral=True)

    panel = pres.data[0]
    panel_id = int(panel["id"])

    try:
        await db_to_thread(lambda: delete_slots(panel_id))
        await db_to_thread(lambda: sb.table("panels").delete().eq("id", panel_id).execute())
    except Exception as e:
        return await interaction.followup.send(f"❌ 削除失敗: {e}", ephemeral=True)

    await interaction.followup.send(f"✅ {day_label(day)} を削除した！", ephemeral=True)


# ========= lifecycle =========
@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


async def main():
    # 429対策：起動直後の連打を避ける
    await asyncio.sleep(5)
    await client.start(TOKEN)


asyncio.run(main())