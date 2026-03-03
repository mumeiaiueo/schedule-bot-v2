import os
import asyncio
from datetime import datetime, timedelta, timezone

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

# setup中の一時状態（メモリ）
draft: dict[tuple[str, str], dict] = {}  # (guild_id, user_id) -> state

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

# =========================================================
# Schema feature detection
# =========================================================
HAS_NOTIFY_ENABLED = True  # panels.notify_enabled があるか

def detect_schema_features():
    global HAS_NOTIFY_ENABLED
    try:
        sb.table("panels").select("notify_enabled").limit(1).execute()
        HAS_NOTIFY_ENABLED = True
    except Exception as e:
        # PGRST204 などで列が無い場合
        if "notify_enabled" in str(e):
            HAS_NOTIFY_ENABLED = False
        else:
            # 予期しないエラーは一旦 False 扱い（落とさない）
            HAS_NOTIFY_ENABLED = False

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

def db_update_slot_atomic_reserve(slot_id: int, user_id: str, name: str):
    # reserved_by が NULL の時だけ予約（競合防止）
    patch = {
        "reserved_by": user_id,
        "reserver_user_id": int(user_id),
        "reserver_name": name,
        "reserved_at": datetime.now(UTC).isoformat(),
        "notified": False,
    }
    return (
        sb.table("slots")
        .update(patch)
        .eq("id", slot_id)
        .is_("reserved_by", "null")
        .execute()
    )

def db_update_slot_atomic_cancel(slot_id: int, user_id: str):
    patch = {
        "reserved_by": None,
        "reserver_user_id": None,
        "reserver_name": None,
        "reserved_at": None,
        "notified": False,
    }
    return (
        sb.table("slots")
        .update(patch)
        .eq("id", slot_id)
        .eq("reserved_by", user_id)
        .execute()
    )

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
    if interaction.user.guild_permissions.administrator:
        return True
    gid = str(interaction.guild_id)
    rid = await db_to_thread(lambda: db_get_manager_role_id(gid))
    if not rid:
        return False
    if isinstance(interaction.user, discord.Member):
        return any(r.id == int(rid) for r in interaction.user.roles)
    return False

# =========================================================
# Setup Wizard UI (Step1/Step2)
# =========================================================
def build_setup_embed(st: dict) -> discord.Embed:
    step = int(st.get("step", 1))
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)
    e.description = "ボタン/セレクトで設定してね👇"

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

    e.set_footer(text="Step1→『次へ』 / Step2→『作成』")
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
        # 可能なら元のメッセージも更新（環境によってeditできない時は握る）
        try:
            await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupWizardView(self.st))
        except Exception:
            await interaction.response.send_message("✅ タイトルを反映したよ（元の画面で次の操作をすると表示も更新される）", ephemeral=True)

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
        # Row0: 今日/明日/次へ（ボタンは幅1、最大5個までOK）
        day_key = self.st.get("day_key", "today")
        today_style = discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary
        tom_style = discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary

        b_today = discord.ui.Button(label="今日", style=today_style, custom_id="setup:day:today", row=0)
        b_tom = discord.ui.Button(label="明日", style=tom_style, custom_id="setup:day:tomorrow", row=0)
        b_next = discord.ui.Button(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=0)

        b_today.callback = self._on_day_today
        b_tom.callback = self._on_day_tomorrow
        b_next.callback = self._on_next

        self.add_item(b_today)
        self.add_item(b_tom)
        self.add_item(b_next)

        # Select は幅5（1行に1個だけ）
        sh, sm = self.st.get("start_h"), self.st.get("start_m")
        eh, em = self.st.get("end_h"), self.st.get("end_m")

        ph_sh = f"開始(時){'=' + str(sh).zfill(2) if sh is not None else ''}"
        ph_sm = f"開始(分){'=' + str(sm).zfill(2) if sm is not None else ''}"
        ph_eh = f"終了(時){'=' + str(eh).zfill(2) if eh is not None else ''}"
        ph_em = f"終了(分){'=' + str(em).zfill(2) if em is not None else ''}"

        sel_sh = discord.ui.Select(custom_id="setup:start_h", placeholder=ph_sh, options=_set_defaults(_opt_nums(24), sh), row=1)
        sel_sm = discord.ui.Select(custom_id="setup:start_m", placeholder=ph_sm, options=_set_defaults(_opt_nums(60, 5), sm), row=2)
        sel_eh = discord.ui.Select(custom_id="setup:end_h", placeholder=ph_eh, options=_set_defaults(_opt_nums(24), eh), row=3)
        sel_em = discord.ui.Select(custom_id="setup:end_m", placeholder=ph_em, options=_set_defaults(_opt_nums(60, 5), em), row=4)

        sel_sh.callback = self._on_select
        sel_sm.callback = self._on_select
        sel_eh.callback = self._on_select
        sel_em.callback = self._on_select

        self.add_item(sel_sh)
        self.add_item(sel_sm)
        self.add_item(sel_eh)
        self.add_item(sel_em)

    def _build_step2(self):
        # Row0: 間隔 select（幅5なので単独）
        interval = self.st.get("interval_minutes")
        ph_interval = f"間隔（20/25/30）{'=' + str(interval) if interval else ''}"
        sel_interval = discord.ui.Select(
            custom_id="setup:interval",
            placeholder=ph_interval,
            options=[
                discord.SelectOption(label="20分", value="20", default=(interval == 20)),
                discord.SelectOption(label="25分", value="25", default=(interval == 25)),
                discord.SelectOption(label="30分", value="30", default=(interval == 30)),
            ],
            row=0
        )
        sel_interval.callback = self._on_select
        self.add_item(sel_interval)

        # Row1: タイトル / everyone（ボタン2つ）
        b_title = discord.ui.Button(label="📝 タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=1)
        b_title.callback = self._on_title
        self.add_item(b_title)

        ev_on = bool(self.st.get("mention_everyone", False))
        ev_style = discord.ButtonStyle.danger if ev_on else discord.ButtonStyle.secondary
        ev_label = "@everyone ON" if ev_on else "@everyone OFF"
        b_ev = discord.ui.Button(label=ev_label, style=ev_style, custom_id="setup:everyone", row=1)
        b_ev.callback = self._on_everyone
        self.add_item(b_ev)

        # Row2: 通知チャンネル（幅5で単独）
        cs = discord.ui.ChannelSelect(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（未選択=このチャンネル）",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=2
        )
        cs.callback = self._on_channel_select
        self.add_item(cs)

        # Row3: 戻る / 作成
        b_back = discord.ui.Button(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=3)
        b_create = discord.ui.Button(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=3)
        b_back.callback = self._on_back
        b_create.callback = self._on_create
        self.add_item(b_back)
        self.add_item(b_create)

    async def _rerender(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupWizardView(self.st))

    async def _on_day_today(self, interaction: discord.Interaction):
        self.st["day_key"] = "today"
        await self._rerender(interaction)

    async def _on_day_tomorrow(self, interaction: discord.Interaction):
        self.st["day_key"] = "tomorrow"
        await self._rerender(interaction)

    async def _on_next(self, interaction: discord.Interaction):
        self.st["step"] = 2
        await self._rerender(interaction)

    async def _on_back(self, interaction: discord.Interaction):
        self.st["step"] = 1
        await self._rerender(interaction)

    async def _on_select(self, interaction: discord.Interaction):
        cid = interaction.data.get("custom_id")  # type: ignore
        val = (interaction.data.get("values") or [None])[0]  # type: ignore
        if val is None:
            await self._rerender(interaction)
            return

        if cid == "setup:start_h":
            self.st["start_h"] = int(val)
        elif cid == "setup:start_m":
            self.st["start_m"] = int(val)
        elif cid == "setup:end_h":
            self.st["end_h"] = int(val)
        elif cid == "setup:end_m":
            self.st["end_m"] = int(val)
        elif cid == "setup:interval":
            self.st["interval_minutes"] = int(val)

        await self._rerender(interaction)

    async def _on_channel_select(self, interaction: discord.Interaction):
        vals = interaction.data.get("values") or []  # type: ignore
        if vals:
            self.st["notify_channel_id"] = str(vals[0])
        await self._rerender(interaction)

    async def _on_title(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TitleModal(self.st))

    async def _on_everyone(self, interaction: discord.Interaction):
        self.st["mention_everyone"] = not bool(self.st.get("mention_everyone", False))
        await self._rerender(interaction)

    async def _on_create(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        sh, sm = self.st.get("start_h"), self.st.get("start_m")
        eh, em = self.st.get("end_h"), self.st.get("end_m")
        interval = self.st.get("interval_minutes")

        if None in (sh, sm, eh, em) or not interval:
            await interaction.followup.send("❌ 開始/終了/間隔が未選択。Step1/Step2を全部埋めてね", ephemeral=True)
            return

        day_key = self.st.get("day_key", "today")
        title = self.st.get("title") or "無題"
        mention_everyone = bool(self.st.get("mention_everyone", False))
        notify_channel_id = self.st.get("notify_channel_id") or str(interaction.channel_id)

        base = datetime.now(JST).date()
        if day_key == "tomorrow":
            base = base + timedelta(days=1)

        start_dt = datetime(base.year, base.month, base.day, int(sh), int(sm), tzinfo=JST)
        end_dt = datetime(base.year, base.month, base.day, int(eh), int(em), tzinfo=JST)
        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)

        start_hm = start_dt.strftime("%H:%M")
        end_hm = end_dt.strftime("%H:%M")

        # panels保存（notify_enabled は列がある時だけ）
        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),          # 公開パネル投稿先
            "day_key": day_key,
            "title": title,
            "interval_minutes": int(interval),
            "notify_channel_id": str(notify_channel_id),        # 3分前通知先
            "mention_everyone": bool(mention_everyone),

            "start_h": int(sh), "start_m": int(sm),
            "end_h": int(eh), "end_m": int(em),
            "start_hm": start_hm,
            "end_hm": end_hm,

            "created_by": str(interaction.user.id),
            "created_at": datetime.now(UTC).isoformat(),
        }
        if HAS_NOTIFY_ENABLED:
            row["notify_enabled"] = True

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

        # slots作成（既存は削除）
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
                "slot_time": cur.strftime("%H:%M"),     # NOT NULL
                "is_break": False,
                "notified": False,
                "reserved_by": None,
                "reserver_user_id": None,
                "reserver_name": None,
                "reserved_at": None,
            })
            cur += timedelta(minutes=int(interval))

        try:
            await db_to_thread(lambda: db_insert_slots(slot_rows))
        except Exception as e:
            await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)
            return

        # 公開パネル投稿（枠投稿先 = /setup 実行チャンネル）
        slots = (await db_to_thread(lambda: db_get_slots(panel_id))).data or []
        view = await SlotsView.build(panel_id)
        msg = await interaction.channel.send(
            content=f"📅 **{title}**（{'今日' if day_key=='today' else '明日'}） / interval {interval}min\n下のボタンで予約してね👇",
            embed=build_panel_embed(title, day_key, int(interval), slots),
            view=view,
        )

        # message_id保存（再起動後もボタン生かす用）
        try:
            await db_to_thread(lambda: db_update_panel(panel_id, {"panel_message_id": str(msg.id)}))
        except Exception:
            pass

        # 作成時 @everyone 1回だけ
        if mention_everyone:
            try:
                await interaction.channel.send("@everyone 募集を開始しました！")
                await db_to_thread(lambda: db_update_panel(panel_id, {"mention_everyone": False}))
            except Exception:
                pass

        await interaction.followup.send("✅ 保存して、公開パネルを投稿した！", ephemeral=True)

# =========================================================
# Panel Embed & Slots View
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

class SlotsView(discord.ui.View):
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)
        self.panel_id = panel_id

    @classmethod
    async def build(cls, panel_id: int) -> "SlotsView":
        res = await db_to_thread(lambda: db_get_slots(panel_id))
        slots = res.data or []

        v = cls(panel_id)

        # 20枠を 4行×5列
        for idx, s in enumerate(slots[:20]):
            slot_id = int(s["id"])
            t = s.get("slot_time") or "??:??"
            is_break = bool(s.get("is_break", False))
            reserved_by = s.get("reserved_by")

            row = idx // 5
            style = discord.ButtonStyle.success
            if is_break:
                style = discord.ButtonStyle.secondary
            elif reserved_by:
                style = discord.ButtonStyle.danger

            btn = SlotButton(panel_id=panel_id, slot_id=slot_id, label=t, style=style, row=row)
            v.add_item(btn)

        # Row4: 管理系（最大5個まで）
        notify_btn = NotifyToggleButton(panel_id)
        if not HAS_NOTIFY_ENABLED:
            notify_btn.disabled = True
            notify_btn.style = discord.ButtonStyle.secondary
            notify_btn.label = "🔔 通知ON(固定)"
        else:
            # 現在値反映
            pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
            cur = True
            if pres.data and pres.data[0].get("notify_enabled") is not None:
                cur = bool(pres.data[0]["notify_enabled"])
            notify_btn.style = discord.ButtonStyle.success if cur else discord.ButtonStyle.secondary
            notify_btn.label = "🔔 通知ON" if cur else "🔕 通知OFF"

        v.add_item(notify_btn)
        v.add_item(BreakToggleButton(panel_id))
        v.add_item(DeletePanelButton(panel_id))

        return v

class SlotButton(discord.ui.Button):
    def __init__(self, panel_id: int, slot_id: int, label: str, style: discord.ButtonStyle, row: int):
        super().__init__(label=label, style=style, custom_id=f"slot:{slot_id}", row=row)
        self.panel_id = panel_id
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        sres = await db_to_thread(lambda: db_get_slot(self.slot_id))
        if not sres.data:
            await interaction.followup.send("❌ その枠が見つからない", ephemeral=True)
            return
        slot = sres.data[0]

        if bool(slot.get("is_break", False)):
            await interaction.followup.send("❌ 休憩枠は予約できない", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        reserved_by = slot.get("reserved_by")

        # 他人予約は不可
        if reserved_by and reserved_by != user_id:
            await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
            return

        # 自分ならキャンセル（原子更新）
        if reserved_by == user_id:
            cres = await db_to_thread(lambda: db_update_slot_atomic_cancel(self.slot_id, user_id))
            if not (cres.data or []):
                await interaction.followup.send("❌ キャンセル失敗（競合）", ephemeral=True)
                return
            await interaction.followup.send("✅ キャンセルしたよ", ephemeral=True)

        # 空きなら予約（原子更新）
        else:
            rres = await db_to_thread(lambda: db_update_slot_atomic_reserve(self.slot_id, user_id, interaction.user.display_name))
            if not (rres.data or []):
                await interaction.followup.send("❌ 直前で埋まりました（競合）", ephemeral=True)
                return
            await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

        await refresh_panel_message(interaction, self.panel_id)

class NotifyToggleButton(discord.ui.Button):
    def __init__(self, panel_id: int):
        super().__init__(label="🔔 通知", style=discord.ButtonStyle.success, custom_id=f"notify:{panel_id}", row=4)
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction):
        if not HAS_NOTIFY_ENABLED:
            await interaction.response.send_message(
                "❌ DBに notify_enabled 列が無いので切替できないよ。\n"
                "SQL: `alter table public.panels add column if not exists notify_enabled boolean not null default true;`",
                ephemeral=True
            )
            return

        if not await is_manager(interaction):
            await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        pres = await db_to_thread(lambda: sb.table("panels").select("notify_enabled").eq("id", self.panel_id).limit(1).execute())
        cur = True
        if pres.data and pres.data[0].get("notify_enabled") is not None:
            cur = bool(pres.data[0]["notify_enabled"])

        try:
            await db_to_thread(lambda: db_update_panel(self.panel_id, {"notify_enabled": (not cur)}))
        except Exception as e:
            await interaction.followup.send(f"❌ notify_enabled更新失敗: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"✅ 通知を {'ON' if (not cur) else 'OFF'} にした", ephemeral=True)
        await refresh_panel_message(interaction, self.panel_id)

class BreakToggleButton(discord.ui.Button):
    def __init__(self, panel_id: int):
        super().__init__(label="🛠 休憩切替", style=discord.ButtonStyle.secondary, custom_id=f"break:{panel_id}", row=4)
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction):
        if not await is_manager(interaction):
            await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        sres = await db_to_thread(lambda: db_get_slots(self.panel_id))
        slots = sres.data or []
        opts = []
        for s in slots[:25]:
            t = s.get("slot_time") or "??:??"
            is_break = bool(s.get("is_break", False))
            reserved_by = s.get("reserved_by")
            if reserved_by:
                continue  # 予約済みは対象外
            mark = "⚪" if is_break else "🟢"
            opts.append(discord.SelectOption(label=f"{mark} {t}", value=str(s["id"])))

        if not opts:
            await interaction.followup.send("❌ 休憩にできる枠がない（予約済みは不可）", ephemeral=True)
            return

        view = BreakSelectView(self.panel_id, opts)
        await interaction.followup.send("休憩にする/戻す枠を選んでね👇", view=view, ephemeral=True)

class BreakSelectView(discord.ui.View):
    def __init__(self, panel_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=120)
        self.panel_id = panel_id
        sel = discord.ui.Select(
            custom_id=f"breaksel:{panel_id}",
            placeholder="枠を選択",
            options=options,
            min_values=1, max_values=1
        )
        sel.callback = self._on_pick
        self.add_item(sel)

    async def _on_pick(self, interaction: discord.Interaction):
        if not await is_manager(interaction):
            await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        slot_id = int((interaction.data.get("values") or [0])[0])  # type: ignore

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

        await refresh_panel_message(interaction, self.panel_id)

class DeletePanelButton(discord.ui.Button):
    def __init__(self, panel_id: int):
        super().__init__(label="🗑 削除", style=discord.ButtonStyle.danger, custom_id=f"del:{panel_id}", row=4)
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction):
        if not await is_manager(interaction):
            await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        pres = await db_to_thread(lambda: sb.table("panels").select("guild_id,day_key").eq("id", self.panel_id).limit(1).execute())
        if not pres.data:
            await interaction.followup.send("❌ panels が見つからない", ephemeral=True)
            return

        guild_id = pres.data[0]["guild_id"]
        day_key = pres.data[0]["day_key"]

        try:
            await db_to_thread(lambda: db_delete_slots(self.panel_id))
            await db_to_thread(lambda: db_delete_panel(guild_id, day_key))
        except Exception as e:
            await interaction.followup.send(f"❌ 削除失敗: {e}", ephemeral=True)
            return

        try:
            await interaction.message.delete()
        except Exception:
            pass

        await interaction.followup.send("✅ パネルを削除した", ephemeral=True)

async def refresh_panel_message(interaction: discord.Interaction, panel_id: int):
    pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
    if not pres.data:
        return
    panel = pres.data[0]

    sres = await db_to_thread(lambda: db_get_slots(panel_id))
    slots = sres.data or []

    title = panel.get("title", "無題")
    day_key = panel.get("day_key", "today")
    interval = int(panel.get("interval_minutes", 30))

    view = await SlotsView.build(panel_id)

    try:
        await interaction.message.edit(
            embed=build_panel_embed(title, day_key, interval, slots),
            view=view
        )
    except Exception:
        pass

# =========================================================
# COMMANDS
# =========================================================
@tree.command(name="setup", description="募集パネルを作る（自分だけ見える設定画面）")
async def setup(interaction: discord.Interaction):
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
    await interaction.response.send_message(
        "設定してね👇（※この画面は自分だけ見える）",
        embed=build_setup_embed(st),
        view=SetupWizardView(st),
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

# =========================================================
# 3分前通知（バックグラウンドループ）
# =========================================================
async def reminder_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            now = datetime.now(UTC)
            window_end = now + timedelta(minutes=3)

            # panels 取得（notify_enabled 列があるなら True のみ）
            if HAS_NOTIFY_ENABLED:
                pres = await db_to_thread(
                    lambda: sb.table("panels")
                    .select("id,notify_channel_id,interval_minutes,notify_enabled")
                    .eq("notify_enabled", True)
                    .execute()
                )
            else:
                pres = await db_to_thread(
                    lambda: sb.table("panels")
                    .select("id,notify_channel_id,interval_minutes")
                    .execute()
                )

            panels = pres.data or []

            for p in panels[:50]:
                panel_id = int(p["id"])
                notify_channel_id = p.get("notify_channel_id")
                if not notify_channel_id:
                    continue

                interval = int(p.get("interval_minutes") or 30)

                sres = await db_to_thread(
                    lambda: sb.table("slots")
                    .select("*")
                    .eq("panel_id", panel_id)
                    .eq("is_break", False)
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

                used: set[int] = set()

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

                    # notified を True
                    for g in group:
                        _id = int(g["id"])
                        try:
                            await db_to_thread(lambda _id=_id: db_update_slot(_id, {"notified": True}))
                        except Exception:
                            pass

        except Exception:
            pass

        await asyncio.sleep(30)

# =========================================================
# READY (sync + persistent views)
# =========================================================
async def register_persistent_panel_views():
    # DBに残ってる panel_message_id のメッセージにViewを再登録して、再起動後もボタン生存
    try:
        pres = await db_to_thread(
            lambda: sb.table("panels").select("id,panel_message_id").is_("panel_message_id", "not.null").execute()
        )
        rows = pres.data or []
        for r in rows[:50]:
            pid = int(r["id"])
            mid = int(r["panel_message_id"])
            view = await SlotsView.build(pid)
            client.add_view(view, message_id=mid)
    except Exception:
        # ここ失敗しても致命ではない
        return

@client.event
async def on_ready():
    # 多重実行防止
    if getattr(client, "_booted", False):
        return
    client._booted = True

    detect_schema_features()

    # コマンドsyncは失敗しても落とさない（429耐性）
    try:
        await asyncio.sleep(2)
        await tree.sync()
    except Exception as e:
        print(f"⚠️ tree.sync 失敗: {e}")

    await register_persistent_panel_views()

    if not getattr(client, "_reminder_started", False):
        client._reminder_started = True
        asyncio.create_task(reminder_loop())

    print(f"✅ Logged in as {client.user} / notify_enabled={HAS_NOTIFY_ENABLED}")

async def main():
    # 429避け（デプロイ直後の連打対策）
    await asyncio.sleep(5)
    await client.start(TOKEN)

asyncio.run(main())