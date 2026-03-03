import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from supabase import create_client


# =========================================================
# LOG
# =========================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("schedule-bot-v2")


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

# setup中の一時状態（メモリ）
draft: dict[tuple[str, str], dict] = {}  # (guild_id, user_id) -> state

# パネル編集の連打対策（同時editを抑制）
_panel_lock: dict[int, asyncio.Lock] = {}
_panel_last_edit: dict[int, float] = {}


# =========================================================
# Helpers
# =========================================================
def dkey(interaction: discord.Interaction) -> tuple[str, str]:
    return (str(interaction.guild_id), str(interaction.user.id))

async def db_to_thread(fn):
    return await asyncio.to_thread(fn)

def parse_iso(dt_str: str) -> datetime:
    s = str(dt_str).replace("Z", "+00:00").replace(" ", "T")
    return datetime.fromisoformat(s)

def hm_text(h: int | None, m: int | None) -> str | None:
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"

def ensure_lock(panel_id: int) -> asyncio.Lock:
    if panel_id not in _panel_lock:
        _panel_lock[panel_id] = asyncio.Lock()
    return _panel_lock[panel_id]


# =========================================================
# DB helpers
# =========================================================
def db_upsert_panel(row: dict):
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

def db_get_panel_by_id(panel_id: int):
    return sb.table("panels").select("*").eq("id", panel_id).limit(1).execute()

def db_delete_panel(guild_id: str, day_key: str):
    return sb.table("panels").delete().eq("guild_id", guild_id).eq("day_key", day_key).execute()

def db_delete_slots(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()

def db_insert_slots(rows: list[dict]):
    return sb.table("slots").insert(rows).execute()

def db_get_slots(panel_id: int):
    return sb.table("slots").select("*").eq("panel_id", panel_id).order("start_at").execute()

def db_get_slot(slot_id: int):
    return sb.table("slots").select("*").eq("id", slot_id).limit(1).execute()

def db_update_slot(slot_id: int, patch: dict):
    return sb.table("slots").update(patch).eq("id", slot_id).execute()

def db_update_panel(panel_id: int, patch: dict):
    return sb.table("panels").update(patch).eq("id", panel_id).execute()

# ---- guild_settings (管理ロール) ----
def db_get_manager_role_id(guild_id: str):
    res = sb.table("guild_settings").select("manager_role_id").eq("guild_id", guild_id).limit(1).execute()
    if res.data:
        return res.data[0].get("manager_role_id")
    return None

def db_set_manager_role_id(guild_id: str, role_id: int | None):
    row = {"guild_id": guild_id, "manager_role_id": role_id}
    return sb.table("guild_settings").upsert(row, on_conflict="guild_id").execute()

async def is_manager(interaction: discord.Interaction) -> bool:
    # 管理者は常にOK
    if interaction.user.guild_permissions.administrator:
        return True
    # 指定ロール保持者もOK
    gid = str(interaction.guild_id)
    rid = await db_to_thread(lambda: db_get_manager_role_id(gid))
    if not rid:
        return False
    if isinstance(interaction.user, discord.Member):
        return any(r.id == int(rid) for r in interaction.user.roles)
    return False


# =========================================================
# Setup UI (Step1 / Step2)
# =========================================================
def build_setup_embed(st: dict) -> discord.Embed:
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)

    step = int(st.get("step", 1))
    e.add_field(name="Step", value=str(step), inline=True)

    day_key = st.get("day_key", "today")
    day_label = "今日" if day_key == "today" else "明日"
    e.add_field(name="日付", value=day_label, inline=True)

    start = hm_text(st.get("start_h"), st.get("start_m"))
    end = hm_text(st.get("end_h"), st.get("end_m"))
    e.add_field(name="開始", value=(start or "未選択"), inline=True)
    e.add_field(name="終了", value=(end or "未選択"), inline=True)

    interval = st.get("interval_minutes")
    e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)

    title = st.get("title") or "無題"
    e.add_field(name="タイトル", value=title, inline=False)

    notify = st.get("notify_channel_id")
    e.add_field(
        name="通知チャンネル（3分前通知）",
        value=(f"<#{notify}>" if notify else "未選択=このチャンネル"),
        inline=False
    )

    everyone = bool(st.get("mention_everyone", False))
    e.add_field(name="@everyone（作成時1回）", value=("ON" if everyone else "OFF"), inline=True)

    if step == 1:
        e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    else:
        e.set_footer(text="Step1→「次へ」 / Step2→「作成」")

    return e

def _opt_nums(n: int, step: int = 1):
    return [discord.SelectOption(label=f"{i:02d}", value=str(i)) for i in range(0, n, step)]

def _set_defaults(options: list[discord.SelectOption], selected_value: int | None):
    if selected_value is None:
        return options
    for o in options:
        if o.value == str(selected_value):
            o.default = True
    return options

class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False)

    def __init__(self, st: dict):
        super().__init__(timeout=300)
        self.st = st

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        # ※モーダル送信だけでは元メッセージを編集できないので、次に何か選ぶと反映されます
        await interaction.response.send_message("✅ タイトルを保存したよ（次の操作で画面にも反映される）", ephemeral=True)

# ---- No-op items (on_interactionで処理するため) ----
class NoopButton(discord.ui.Button):
    async def callback(self, interaction: discord.Interaction):
        return

class NoopSelect(discord.ui.Select):
    async def callback(self, interaction: discord.Interaction):
        return

class NoopChannelSelect(discord.ui.ChannelSelect):
    async def callback(self, interaction: discord.Interaction):
        return

def build_setup_view(st: dict) -> discord.ui.View:
    step = int(st.get("step", 1))
    day_key = st.get("day_key", "today")

    v = discord.ui.View(timeout=600)

    # 共通：日付ボタン（選択した方を強調）
    btn_today_style = discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary
    btn_tom_style = discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary
    v.add_item(NoopButton(label="今日", style=btn_today_style, custom_id="setup:day:today", row=0))
    v.add_item(NoopButton(label="明日", style=btn_tom_style, custom_id="setup:day:tomorrow", row=0))

    if step == 1:
        sh, sm = st.get("start_h"), st.get("start_m")
        eh, em = st.get("end_h"), st.get("end_m")

        v.add_item(NoopSelect(
            custom_id="setup:start_h",
            placeholder="開始(時)",
            options=_set_defaults(_opt_nums(24), sh),
            row=1
        ))
        v.add_item(NoopSelect(
            custom_id="setup:start_m",
            placeholder="開始(分)",
            options=_set_defaults(_opt_nums(60, step=5), sm),
            row=1
        ))
        v.add_item(NoopSelect(
            custom_id="setup:end_h",
            placeholder="終了(時)",
            options=_set_defaults(_opt_nums(24), eh),
            row=2
        ))
        v.add_item(NoopSelect(
            custom_id="setup:end_m",
            placeholder="終了(分)",
            options=_set_defaults(_opt_nums(60, step=5), em),
            row=2
        ))

        v.add_item(NoopButton(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=3))
        return v

    # step == 2
    interval = st.get("interval_minutes")
    v.add_item(NoopSelect(
        custom_id="setup:interval",
        placeholder="間隔（20/25/30）",
        options=[
            discord.SelectOption(label="20分", value="20", default=(interval == 20)),
            discord.SelectOption(label="25分", value="25", default=(interval == 25)),
            discord.SelectOption(label="30分", value="30", default=(interval == 30)),
        ],
        row=1
    ))

    v.add_item(NoopButton(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=2))

    ev_on = bool(st.get("mention_everyone", False))
    ev_style = discord.ButtonStyle.danger if ev_on else discord.ButtonStyle.secondary
    ev_label = "@everyone ON" if ev_on else "@everyone OFF"
    v.add_item(NoopButton(label=ev_label, style=ev_style, custom_id="setup:everyone", row=2))

    v.add_item(NoopChannelSelect(
        custom_id="setup:notify_channel",
        placeholder="通知チャンネル（未選択=このチャンネル）",
        min_values=1, max_values=1,
        channel_types=[discord.ChannelType.text],
        row=3
    ))

    v.add_item(NoopButton(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=4))
    v.add_item(NoopButton(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=4))
    return v


# =========================================================
# Panel Embed & Panel View
# =========================================================
def build_panel_embed(title: str, day_key: str, interval: int, slots: list[dict]) -> discord.Embed:
    e = discord.Embed(title="募集パネル", color=0x2B2D31)
    day_label = "今日" if day_key == "today" else "明日"
    e.description = f"📅 {day_label}（JST） / interval {interval}min"

    lines = []
    for s in slots[:30]:
        t = s.get("slot_time") or "??:??"
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

def build_panel_view(panel_id: int, slots: list[dict], notify_enabled: bool) -> discord.ui.View:
    v = discord.ui.View(timeout=None)

    for s in slots[:20]:
        t = s.get("slot_time") or "??:??"
        is_break = bool(s.get("is_break", False))
        reserved_by = s.get("reserved_by")

        if is_break:
            style = discord.ButtonStyle.secondary
        elif reserved_by:
            style = discord.ButtonStyle.danger
        else:
            style = discord.ButtonStyle.success

        v.add_item(NoopButton(label=t, style=style, custom_id=f"slot:{int(s['id'])}", row=0))

    # 下部操作
    n_label = "🔔 通知ON" if notify_enabled else "🔕 通知OFF"
    n_style = discord.ButtonStyle.success if notify_enabled else discord.ButtonStyle.secondary
    v.add_item(NoopButton(label=n_label, style=n_style, custom_id=f"notify:{panel_id}", row=1))
    v.add_item(NoopButton(label="🛠 休憩切替（管理者/管理ロール）", style=discord.ButtonStyle.secondary, custom_id=f"break:{panel_id}", row=2))
    v.add_item(NoopButton(label="🗑 削除（管理者/管理ロール）", style=discord.ButtonStyle.danger, custom_id=f"del:{panel_id}", row=3))
    return v


async def refresh_panel_message(message: discord.Message, panel_id: int):
    lock = ensure_lock(panel_id)
    async with lock:
        now = asyncio.get_event_loop().time()
        last = _panel_last_edit.get(panel_id, 0.0)
        # edit連打抑制（1秒以内は少し待つ）
        if now - last < 1.0:
            await asyncio.sleep(1.0 - (now - last))

        pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
        if not pres.data:
            return
        panel = pres.data[0]

        sres = await db_to_thread(lambda: db_get_slots(panel_id))
        slots = sres.data or []

        title = panel.get("title", "無題")
        day_key = panel.get("day_key", "today")
        interval = int(panel.get("interval_minutes", 30))

        notify_enabled = True
        if panel.get("notify_enabled") is not None:
            notify_enabled = bool(panel["notify_enabled"])

        try:
            await message.edit(
                embed=build_panel_embed(title, day_key, interval, slots),
                view=build_panel_view(panel_id, slots, notify_enabled),
            )
            _panel_last_edit[panel_id] = asyncio.get_event_loop().time()
        except Exception:
            pass


# =========================================================
# BREAK SELECT (ephemeral)
# =========================================================
def build_break_select_view(panel_id: int, slots: list[dict]) -> discord.ui.View:
    opts = []
    for s in slots[:25]:
        t = s.get("slot_time") or "??:??"
        is_break = bool(s.get("is_break", False))
        mark = "⚪" if is_break else "🟢"
        opts.append(discord.SelectOption(label=f"{mark} {t}", value=str(int(s["id"]))))

    v = discord.ui.View(timeout=120)
    v.add_item(NoopSelect(
        custom_id=f"breaksel:{panel_id}",
        placeholder="休憩にする/戻す枠を選択",
        options=opts,
        min_values=1,
        max_values=1,
        row=0
    ))
    return v


# =========================================================
# COMMANDS
# =========================================================
@tree.command(name="setup", description="募集パネルを作る（自分だけ見える設定画面）")
async def setup(interaction: discord.Interaction):
    key = dkey(interaction)
    draft[key] = {
        "step": 1,
        "day_key": "today",   # 初期は今日（選ばなくてOK）
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
    }
    st = draft[key]
    await interaction.response.send_message(
        "設定して進めてね👇（※この画面は自分だけ見える）",
        embed=build_setup_embed(st),
        view=build_setup_view(st),
        ephemeral=True
    )

@tree.command(name="manager_role", description="管理ロールを設定/解除（管理者のみ）")
@app_commands.describe(role="管理ロール（解除したいときは未選択で実行）")
async def manager_role(interaction: discord.Interaction, role: discord.Role | None = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ サーバー管理者のみ実行できます", ephemeral=True)
        return

    gid = str(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)

    try:
        await db_to_thread(lambda: db_set_manager_role_id(gid, int(role.id) if role else None))
    except Exception as e:
        await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
        return

    if role:
        await interaction.followup.send(f"✅ 管理ロールを {role.mention} に設定した", ephemeral=True)
    else:
        await interaction.followup.send("✅ 管理ロールを解除した", ephemeral=True)

@tree.command(name="reset", description="今日/明日の募集を削除（管理者/管理ロール）")
async def reset(interaction: discord.Interaction):
    if not await is_manager(interaction):
        await interaction.response.send_message("❌ 管理者/管理ロールのみ実行できます", ephemeral=True)
        return

    v = discord.ui.View(timeout=60)
    v.add_item(NoopButton(label="今日を削除", style=discord.ButtonStyle.danger, custom_id="reset:today", row=0))
    v.add_item(NoopButton(label="明日を削除", style=discord.ButtonStyle.danger, custom_id="reset:tomorrow", row=0))
    await interaction.response.send_message("どっちを削除する？", view=v, ephemeral=True)


# =========================================================
# COMPONENT HANDLER（ここだけ見ればOK）
# =========================================================
@client.event
async def on_interaction(interaction: discord.Interaction):
    try:
        # スラッシュは tree へ
        if interaction.type == discord.InteractionType.application_command:
            await tree._call(interaction)
            return

        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data or {}
        cid = (data.get("custom_id") or "").strip()
        if not cid:
            return

        # -------------------------
        # SETUP WIZARD
        # -------------------------
        if cid.startswith("setup:"):
            key = dkey(interaction)
            st = draft.get(key)
            if not st:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ /setup からやり直してね", ephemeral=True)
                return

            # day
            if cid == "setup:day:today":
                st["day_key"] = "today"
                st.setdefault("step", 1)
                await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return
            if cid == "setup:day:tomorrow":
                st["day_key"] = "tomorrow"
                st.setdefault("step", 1)
                await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return

            # next/back
            if cid == "setup:next":
                st["step"] = 2
                await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return
            if cid == "setup:back":
                st["step"] = 1
                await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return

            # title modal
            if cid == "setup:title":
                await interaction.response.send_modal(TitleModal(st))
                return

            # everyone toggle
            if cid == "setup:everyone":
                st["mention_everyone"] = not bool(st.get("mention_everyone", False))
                await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return

            # selects / channelselect
            values = data.get("values") or []
            if cid in ("setup:start_h", "setup:start_m", "setup:end_h", "setup:end_m", "setup:interval", "setup:notify_channel") and values:
                v0 = values[0]
                if cid == "setup:start_h":
                    st["start_h"] = int(v0)
                elif cid == "setup:start_m":
                    st["start_m"] = int(v0)
                elif cid == "setup:end_h":
                    st["end_h"] = int(v0)
                elif cid == "setup:end_m":
                    st["end_m"] = int(v0)
                elif cid == "setup:interval":
                    st["interval_minutes"] = int(v0)
                elif cid == "setup:notify_channel":
                    st["notify_channel_id"] = str(v0)

                await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return

            # create
            if cid == "setup:create":
                await interaction.response.defer(ephemeral=True)

                sh, sm = st.get("start_h"), st.get("start_m")
                eh, em = st.get("end_h"), st.get("end_m")
                interval = st.get("interval_minutes")

                if None in (sh, sm, eh, em) or not interval:
                    await interaction.followup.send("❌ 開始/終了/間隔が未選択。/setup からやり直してね", ephemeral=True)
                    return

                day_key = st.get("day_key", "today")
                title = st.get("title") or "無題"
                mention_everyone = bool(st.get("mention_everyone", False))

                # ✅ 通知チャンネルは「3分前通知用」
                notify_channel_id = st.get("notify_channel_id") or str(interaction.channel_id)

                # 日付確定
                base = datetime.now(JST).date()
                if day_key == "tomorrow":
                    base = base + timedelta(days=1)

                start_dt = datetime(base.year, base.month, base.day, int(sh), int(sm), tzinfo=JST)
                end_dt = datetime(base.year, base.month, base.day, int(eh), int(em), tzinfo=JST)
                if end_dt <= start_dt:
                    end_dt = end_dt + timedelta(days=1)

                start_hm = start_dt.strftime("%H:%M")
                end_hm = end_dt.strftime("%H:%M")

                # panels 保存（schema事故回避：start_at/end_atに依存しない）
                row = {
                    "guild_id": str(interaction.guild_id),
                    "channel_id": str(interaction.channel_id),   # 公開パネル投稿先（ここ）
                    "day_key": day_key,
                    "title": title,
                    "interval_minutes": int(interval),

                    "notify_channel_id": str(notify_channel_id), # ✅ 3分前通知先
                    "mention_everyone": bool(mention_everyone),

                    "notify_enabled": True,

                    "start_h": int(sh), "start_m": int(sm),
                    "end_h": int(eh), "end_m": int(em),
                    "start_hm": start_hm,
                    "end_hm": end_hm,

                    "created_by": str(interaction.user.id),
                    "created_at": datetime.now(UTC).isoformat(),
                }

                try:
                    pres = await db_to_thread(lambda: db_upsert_panel(row))
                except Exception as e:
                    await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
                    return

                panel = pres.data[0] if pres.data else None
                if not panel:
                    got = await db_to_thread(lambda: db_get_panel(str(interaction.guild_id), day_key))
                    panel = got.data[0] if got.data else None
                if not panel:
                    await interaction.followup.send("❌ panels 保存後に取得できない。DBを確認してね", ephemeral=True)
                    return

                panel_id = int(panel["id"])

                # slots 作成（既存は削除）
                try:
                    await db_to_thread(lambda: db_delete_slots(panel_id))
                except Exception:
                    pass

                slot_rows = []
                cur = start_dt
                while cur < end_dt:
                    slot_rows.append({
                        "panel_id": panel_id,
                        "start_at": cur.astimezone(UTC).isoformat(),
                        "end_at": (cur + timedelta(minutes=int(interval))).astimezone(UTC).isoformat(),
                        "slot_time": cur.strftime("%H:%M"),  # NOT NULL
                        "is_break": False,
                        "notified": False,
                        "reserved_by": None,
                        "reserver_user_id": None,
                        "reserver_name": None,
                        "reserved_at": None,
                    })
                    cur += timedelta(minutes=int(interval))

                try:
                    ins = await db_to_thread(lambda: db_insert_slots(slot_rows))
                except Exception as e:
                    await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)
                    return

                created = ins.data or []
                if not created:
                    await interaction.followup.send("❌ slots が作れなかった（slots列/制約を確認）", ephemeral=True)
                    return

                # 公開パネル投稿（枠投稿先 = /setup 実行チャンネル）
                ch = interaction.channel

                # notify_enabled 表示
                notify_enabled = True
                if panel.get("notify_enabled") is not None:
                    notify_enabled = bool(panel["notify_enabled"])

                msg = await ch.send(
                    content=f"📅 **{title}**（{'今日' if day_key=='today' else '明日'}） / interval {interval}min\n下のボタンで予約してね👇",
                    embed=build_panel_embed(title, day_key, int(interval), created),
                    view=build_panel_view(panel_id, created, notify_enabled),
                )

                # message_id 保存（任意）
                try:
                    await db_to_thread(lambda: db_update_panel(panel_id, {"panel_message_id": str(msg.id)}))
                except Exception:
                    pass

                # 作成時 @everyone 1回だけ
                if mention_everyone:
                    try:
                        await ch.send("@everyone 募集を開始しました！")
                        await db_to_thread(lambda: db_update_panel(panel_id, {"mention_everyone": False}))
                    except Exception:
                        pass

                await interaction.followup.send("✅ 保存して、公開パネルを投稿した！", ephemeral=True)
                return

            return

        # -------------------------
        # RESET
        # -------------------------
        if cid.startswith("reset:"):
            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            day_key = "today" if cid == "reset:today" else "tomorrow"
            gid = str(interaction.guild_id)

            # message_id を取って消せるなら消す
            pres = await db_to_thread(lambda: db_get_panel(gid, day_key))
            if not pres.data:
                await interaction.followup.send("✅ その日のパネルは見つからなかった（既に無い）", ephemeral=True)
                return
            panel = pres.data[0]
            panel_id = int(panel["id"])
            msg_id = panel.get("panel_message_id")
            ch_id = panel.get("channel_id")

            try:
                await db_to_thread(lambda: db_delete_slots(panel_id))
                await db_to_thread(lambda: db_delete_panel(gid, day_key))
            except Exception as e:
                await interaction.followup.send(f"❌ 削除失敗: {e}", ephemeral=True)
                return

            # 可能ならメッセージも削除
            try:
                if msg_id and ch_id:
                    ch = client.get_channel(int(ch_id))
                    if ch:
                        m = await ch.fetch_message(int(msg_id))
                        await m.delete()
            except Exception:
                pass

            await interaction.followup.send("✅ 削除した", ephemeral=True)
            return

        # -------------------------
        # PANEL BUTTONS
        # -------------------------
        if cid.startswith("slot:"):
            await interaction.response.defer(ephemeral=True)
            slot_id = int(cid.split(":")[1])

            sres = await db_to_thread(lambda: db_get_slot(slot_id))
            if not sres.data:
                await interaction.followup.send("❌ その枠が見つからない", ephemeral=True)
                return
            slot = sres.data[0]

            if bool(slot.get("is_break", False)):
                await interaction.followup.send("❌ 休憩枠は予約できない", ephemeral=True)
                return

            user_id = str(interaction.user.id)
            reserved_by = slot.get("reserved_by")

            if reserved_by and reserved_by != user_id:
                await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
                return

            if reserved_by == user_id:
                patch = {
                    "reserved_by": None,
                    "reserver_user_id": None,
                    "reserver_name": None,
                    "reserved_at": None,
                    "notified": False,
                }
                await db_to_thread(lambda: db_update_slot(slot_id, patch))
                await interaction.followup.send("✅ キャンセルしたよ", ephemeral=True)
            else:
                patch = {
                    "reserved_by": user_id,
                    "reserver_user_id": int(user_id),
                    "reserver_name": interaction.user.display_name,
                    "reserved_at": datetime.now(UTC).isoformat(),
                    "notified": False,
                }
                await db_to_thread(lambda: db_update_slot(slot_id, patch))
                await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

            # panel_id を引く
            panel_id = int(slot["panel_id"])
            await refresh_panel_message(interaction.message, panel_id)
            return

        if cid.startswith("notify:"):
            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)

            panel_id = int(cid.split(":")[1])
            pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
            if not pres.data:
                await interaction.followup.send("❌ panels が見つからない", ephemeral=True)
                return
            panel = pres.data[0]
            cur = True
            if panel.get("notify_enabled") is not None:
                cur = bool(panel["notify_enabled"])

            try:
                await db_to_thread(lambda: db_update_panel(panel_id, {"notify_enabled": (not cur)}))
            except Exception as e:
                await interaction.followup.send(f"❌ notify_enabled更新失敗: {e}", ephemeral=True)
                return

            await interaction.followup.send(f"✅ 通知を {'ON' if (not cur) else 'OFF'} にした", ephemeral=True)
            await refresh_panel_message(interaction.message, panel_id)
            return

        if cid.startswith("break:"):
            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)

            panel_id = int(cid.split(":")[1])
            sres = await db_to_thread(lambda: db_get_slots(panel_id))
            slots = sres.data or []
            if not slots:
                await interaction.followup.send("❌ 枠がない", ephemeral=True)
                return

            await interaction.followup.send(
                "休憩にする/戻す枠を選んでね👇",
                view=build_break_select_view(panel_id, slots),
                ephemeral=True
            )
            return

        if cid.startswith("breaksel:"):
            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)

            panel_id = int(cid.split(":")[1])
            values = data.get("values") or []
            if not values:
                await interaction.followup.send("❌ 選択がない", ephemeral=True)
                return
            slot_id = int(values[0])

            sres = await db_to_thread(lambda: db_get_slot(slot_id))
            if not sres.data:
                await interaction.followup.send("❌ その枠が見つからない", ephemeral=True)
                return
            slot = sres.data[0]

            if slot.get("reserved_by"):
                await interaction.followup.send("❌ 予約済み枠は休憩にできない", ephemeral=True)
                return

            now_break = bool(slot.get("is_break", False))
            await db_to_thread(lambda: db_update_slot(slot_id, {"is_break": (not now_break)}))
            await interaction.followup.send(f"✅ {'休憩にした' if (not now_break) else '休憩解除した'}", ephemeral=True)

            await refresh_panel_message(interaction.message, panel_id)
            return

        if cid.startswith("del:"):
            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)

            panel_id = int(cid.split(":")[1])

            pres = await db_to_thread(lambda: sb.table("panels").select("guild_id,day_key").eq("id", panel_id).limit(1).execute())
            if not pres.data:
                await interaction.followup.send("❌ panels が見つからない", ephemeral=True)
                return

            guild_id = pres.data[0]["guild_id"]
            day_key = pres.data[0]["day_key"]

            try:
                await db_to_thread(lambda: db_delete_slots(panel_id))
                await db_to_thread(lambda: db_delete_panel(guild_id, day_key))
            except Exception as e:
                await interaction.followup.send(f"❌ 削除失敗: {e}", ephemeral=True)
                return

            try:
                await interaction.message.delete()
            except Exception:
                pass

            await interaction.followup.send("✅ パネルを削除した", ephemeral=True)
            return

        # それ以外は無視
        return

    except Exception as e:
        log.exception("on_interaction error")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ エラー: {e}", ephemeral=True)
        except Exception:
            pass


# =========================================================
# 3分前通知（バックグラウンドループ）
# =========================================================
async def reminder_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            now = datetime.now(UTC)
            window_end = now + timedelta(minutes=3)

            # notify_enabled 列が無い/壊れても落ちないように try
            try:
                pres = await db_to_thread(
                    lambda: sb.table("panels")
                    .select("id,notify_channel_id,interval_minutes,notify_enabled")
                    .execute()
                )
                panels = pres.data or []
            except Exception:
                panels = []

            for p in panels[:80]:
                # notify_enabled が無い場合は True 扱い
                if p.get("notify_enabled") is not None and bool(p["notify_enabled"]) is False:
                    continue

                panel_id = int(p["id"])
                notify_channel_id = p.get("notify_channel_id")
                if not notify_channel_id:
                    continue

                interval = int(p.get("interval_minutes") or 30)

                # 3分以内に開始する「予約あり＆未通知」slots
                sres = await db_to_thread(
                    lambda: sb.table("slots")
                    .select("*")
                    .eq("panel_id", panel_id)
                    .is_("reserved_by", "not.null")
                    .eq("notified", False)
                    .gte("start_at", now.isoformat())
                    .lte("start_at", window_end.isoformat())
                    .order("start_at")
                    .execute()
                )
                slots = sres.data or []
                if not slots:
                    continue

                used = set()

                for i, s in enumerate(slots):
                    sid = int(s["id"])
                    if sid in used:
                        continue

                    user_id = s["reserved_by"]
                    st = parse_iso(s["start_at"])
                    en = parse_iso(s["end_at"])

                    group = [s]
                    used.add(sid)

                    last_start = st
                    for t in slots[i+1:]:
                        if t["reserved_by"] != user_id:
                            continue
                        ts = parse_iso(t["start_at"])
                        if ts == last_start + timedelta(minutes=interval):
                            group.append(t)
                            used.add(int(t["id"]))
                            last_start = ts
                            en = parse_iso(t["end_at"])

                    ch = client.get_channel(int(notify_channel_id))
                    if ch is None:
                        continue

                    msg = f"⏰ {st.astimezone(JST).strftime('%H:%M')}〜{en.astimezone(JST).strftime('%H:%M')} の枠です <@{user_id}>"
                    try:
                        await ch.send(msg)
                    except Exception:
                        continue

                    # notified = true
                    try:
                        for x in group:
                            _id = int(x["id"])
                            await db_to_thread(lambda _id=_id: db_update_slot(_id, {"notified": True}))
                    except Exception:
                        pass

        except Exception:
            pass

        await asyncio.sleep(30)


# =========================================================
# READY / START
# =========================================================
@client.event
async def on_ready():
    # sync 多重を防ぐ
    if not getattr(client, "_synced", False):
        try:
            await tree.sync()
            client._synced = True
        except Exception:
            pass

    log.info("✅ Logged in as %s", client.user)

    # reminder_loop 多重を防ぐ
    if not getattr(client, "_reminder_started", False):
        client._reminder_started = True
        asyncio.create_task(reminder_loop())


async def main():
    # 429避け（起動直後の同期/ログイン連打を避ける）
    await asyncio.sleep(5)

    backoff = 10
    while True:
        try:
            await client.start(TOKEN)
            break
        except discord.HTTPException as e:
            # 429 を踏んだら待って再試行（Renderの再起動連打でも死ににくく）
            if getattr(e, "status", None) == 429:
                log.warning("429 rate limited. sleep=%ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            raise

asyncio.run(main())