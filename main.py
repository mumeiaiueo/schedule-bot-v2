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

def _opt_nums(n: int, step: int = 1):
    return [discord.SelectOption(label=f"{i:02d}", value=str(i)) for i in range(0, n, step)]

def _set_defaults(options: list[discord.SelectOption], selected_value: int | None):
    if selected_value is None:
        return options
    for o in options:
        if o.value == str(selected_value):
            o.default = True
    return options

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
def db_get_manager_role_id(guild_id_int: int):
    res = sb.table("guild_settings").select("manager_role_id").eq("guild_id", guild_id_int).limit(1).execute()
    if res.data:
        return res.data[0].get("manager_role_id")
    return None

def db_set_manager_role_id(guild_id_int: int, role_id: int | None):
    row = {"guild_id": guild_id_int, "manager_role_id": role_id}
    return sb.table("guild_settings").upsert(row, on_conflict="guild_id").execute()

async def is_manager(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    # 管理者は常にOK
    if interaction.user.guild_permissions.administrator:
        return True
    gid_int = int(interaction.guild_id)
    rid = await db_to_thread(lambda: db_get_manager_role_id(gid_int))
    if not rid:
        return False
    try:
        rid_int = int(rid)
    except Exception:
        return False
    if isinstance(interaction.user, discord.Member):
        return any(r.id == rid_int for r in interaction.user.roles)
    return False

# =========================================================
# Setup UI (Step1 / Step2)
# =========================================================
def build_setup_embed(st: dict) -> discord.Embed:
    step = int(st.get("step", 1))
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)
    e.description = f"Step {step}"

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

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e

class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False)

    def __init__(self, st: dict):
        super().__init__(timeout=300)
        self.st = st

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        # embed更新（Step2にいるときだけ見た目更新したいが、モーダルでは edit_messageできないのでOK）
        await interaction.response.send_message("✅ タイトルを反映したよ", ephemeral=True)

class SetupStep1View(discord.ui.View):
    def __init__(self, st: dict):
        super().__init__(timeout=600)
        self.st = st

        # Row0: 今日/明日 + 次へ（rowは0〜4しか使えない）
        day_key = st.get("day_key", "today")
        btn_today = discord.ui.Button(
            label="今日",
            style=(discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary),
            custom_id="setup:day:today",
            row=0
        )
        btn_tom = discord.ui.Button(
            label="明日",
            style=(discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary),
            custom_id="setup:day:tomorrow",
            row=0
        )
        btn_next = discord.ui.Button(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=0)

        btn_today.callback = self._on_day_today
        btn_tom.callback = self._on_day_tomorrow
        btn_next.callback = self._on_next
        self.add_item(btn_today)
        self.add_item(btn_tom)
        self.add_item(btn_next)

        # Row1: 開始(時/分)
        sh, sm = st.get("start_h"), st.get("start_m")
        ph_h = f"開始(時) {sh:02d}" if sh is not None else "開始(時)"
        ph_m = f"開始(分) {sm:02d}" if sm is not None else "開始(分)"

        sel_sh = discord.ui.Select(
            custom_id="setup:start_h",
            placeholder=ph_h,
            options=_set_defaults(_opt_nums(24), sh),
            row=1
        )
        sel_sm = discord.ui.Select(
            custom_id="setup:start_m",
            placeholder=ph_m,
            options=_set_defaults(_opt_nums(60, step=5), sm),
            row=1
        )
        sel_sh.callback = self._on_select
        sel_sm.callback = self._on_select
        self.add_item(sel_sh)
        self.add_item(sel_sm)

        # Row2: 終了(時/分)
        eh, em = st.get("end_h"), st.get("end_m")
        ph_eh = f"終了(時) {eh:02d}" if eh is not None else "終了(時)"
        ph_em = f"終了(分) {em:02d}" if em is not None else "終了(分)"

        sel_eh = discord.ui.Select(
            custom_id="setup:end_h",
            placeholder=ph_eh,
            options=_set_defaults(_opt_nums(24), eh),
            row=2
        )
        sel_em = discord.ui.Select(
            custom_id="setup:end_m",
            placeholder=ph_em,
            options=_set_defaults(_opt_nums(60, step=5), em),
            row=2
        )
        sel_eh.callback = self._on_select
        sel_em.callback = self._on_select
        self.add_item(sel_eh)
        self.add_item(sel_em)

    async def _rerender(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupStep1View(self.st))

    async def _on_day_today(self, interaction: discord.Interaction):
        self.st["day_key"] = "today"
        await self._rerender(interaction)

    async def _on_day_tomorrow(self, interaction: discord.Interaction):
        self.st["day_key"] = "tomorrow"
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

        await self._rerender(interaction)

    async def _on_next(self, interaction: discord.Interaction):
        # Step2へ
        self.st["step"] = 2
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupStep2View(self.st))

class SetupStep2View(discord.ui.View):
    def __init__(self, st: dict):
        super().__init__(timeout=600)
        self.st = st

        # Row0: 間隔Select（選択済みを表示）
        interval = st.get("interval_minutes")
        ph = f"{interval}分" if interval else "間隔（20/25/30）"
        sel_interval = discord.ui.Select(
            custom_id="setup:interval",
            placeholder=ph,
            options=[
                discord.SelectOption(label="20分", value="20", default=(interval == 20)),
                discord.SelectOption(label="25分", value="25", default=(interval == 25)),
                discord.SelectOption(label="30分", value="30", default=(interval == 30)),
            ],
            row=0
        )
        sel_interval.callback = self._on_select
        self.add_item(sel_interval)

        # Row1: タイトル + everyone
        btn_title = discord.ui.Button(label="📝 タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=1)
        btn_title.callback = self._on_title
        self.add_item(btn_title)

        ev_on = bool(st.get("mention_everyone", False))
        btn_ev = discord.ui.Button(
            label=("@everyone ON" if ev_on else "@everyone OFF"),
            style=(discord.ButtonStyle.danger if ev_on else discord.ButtonStyle.secondary),
            custom_id="setup:everyone",
            row=1
        )
        btn_ev.callback = self._on_everyone
        self.add_item(btn_ev)

        # Row2: 通知チャンネル（3分前通知）
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
        btn_back = discord.ui.Button(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=3)
        btn_create = discord.ui.Button(label="作成（公開パネル投稿）", style=discord.ButtonStyle.success, custom_id="setup:create", row=3)
        btn_back.callback = self._on_back
        btn_create.callback = self._on_create
        self.add_item(btn_back)
        self.add_item(btn_create)

    async def _rerender(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupStep2View(self.st))

    async def _on_back(self, interaction: discord.Interaction):
        self.st["step"] = 1
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupStep1View(self.st))

    async def _on_select(self, interaction: discord.Interaction):
        cid = interaction.data.get("custom_id")  # type: ignore
        val = (interaction.data.get("values") or [None])[0]  # type: ignore
        if val is None:
            await self._rerender(interaction)
            return

        if cid == "setup:interval":
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
            await interaction.followup.send("❌ 開始/終了/間隔が未選択。もう一度選んでね", ephemeral=True)
            return

        day_key = self.st.get("day_key", "today")
        title = self.st.get("title") or "無題"
        mention_everyone = bool(self.st.get("mention_everyone", False))

        # 通知チャンネル（3分前通知）未選択なら「このチャンネル」
        notify_channel_id = self.st.get("notify_channel_id") or str(interaction.channel_id)

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

        # panels保存（※end_at/start_at は触らない。PGRST204回避）
        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),   # 公開パネル投稿先
            "day_key": day_key,
            "title": title,
            "interval_minutes": int(interval),

            "notify_channel_id": str(notify_channel_id),  # 3分前通知先
            "mention_everyone": bool(mention_everyone),

            "start_h": int(sh), "start_m": int(sm),
            "end_h": int(eh), "end_m": int(em),
            "start_hm": start_hm,
            "end_hm": end_hm,

            "created_by": str(interaction.user.id),
            "created_at": datetime.now(UTC).isoformat(),
        }

        # notify_enabled 列が無い環境でも落ちないように（あれば作成時ON）
        row_with_notify = dict(row)
        row_with_notify["notify_enabled"] = True

        try:
            pres = await db_to_thread(lambda: db_upsert_panel(row_with_notify))
        except Exception as e:
            # notify_enabled が無い等のPGRST204なら、外して再試行
            es = repr(e)
            if "PGRST204" in es and "notify_enabled" in es:
                pres = await db_to_thread(lambda: db_upsert_panel(row))
            else:
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

        # 公開パネル投稿（枠投稿先は /setup 実行チャンネル）
        embed, view = await render_panel(panel_id)
        msg = await interaction.channel.send(
            content=f"📅 **{title}**（{'今日' if day_key=='today' else '明日'}） / interval {interval}min\n下のボタンで予約してね👇",
            embed=embed,
            view=view
        )

        # message_id保存（任意）
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
# Panel render
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

async def build_slots_view(panel_id: int) -> discord.ui.View:
    res = await db_to_thread(lambda: db_get_slots(panel_id))
    slots = res.data or []

    v = discord.ui.View(timeout=None)

    # 20個まで（4行×5列）
    for i, s in enumerate(slots[:20]):
        t = s.get("slot_time") or "??:??"
        is_break = bool(s.get("is_break", False))
        reserved_by = s.get("reserved_by")

        style = discord.ButtonStyle.success
        if is_break:
            style = discord.ButtonStyle.secondary
        elif reserved_by:
            style = discord.ButtonStyle.danger

        btn = discord.ui.Button(
            label=t,
            style=style,
            custom_id=f"slot:{int(s['id'])}",
            row=(i // 5)  # 0〜3
        )
        v.add_item(btn)

    # 管理系（row=4）
    pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
    notify_enabled = True
    if pres.data and pres.data[0].get("notify_enabled") is not None:
        notify_enabled = bool(pres.data[0]["notify_enabled"])

    btn_notify = discord.ui.Button(
        label=("🔔 通知ON" if notify_enabled else "🔕 通知OFF"),
        style=(discord.ButtonStyle.success if notify_enabled else discord.ButtonStyle.secondary),
        custom_id=f"notify:{panel_id}",
        row=4
    )
    btn_break = discord.ui.Button(
        label="🛠 休憩切替（管理者/管理ロール）",
        style=discord.ButtonStyle.secondary,
        custom_id=f"break:{panel_id}",
        row=4
    )
    btn_del = discord.ui.Button(
        label="🗑 削除（管理者/管理ロール）",
        style=discord.ButtonStyle.danger,
        custom_id=f"del:{panel_id}",
        row=4
    )
    v.add_item(btn_notify)
    v.add_item(btn_break)
    v.add_item(btn_del)
    return v

async def render_panel(panel_id: int) -> tuple[discord.Embed, discord.ui.View]:
    pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
    if not pres.data:
        return discord.Embed(title="募集パネル", description="panel not found", color=0x2B2D31), discord.ui.View(timeout=None)
    panel = pres.data[0]

    sres = await db_to_thread(lambda: db_get_slots(panel_id))
    slots = sres.data or []

    title = panel.get("title", "無題")
    day_key = panel.get("day_key", "today")
    interval = int(panel.get("interval_minutes", 30))

    embed = build_panel_embed(title, day_key, interval, slots)
    view = await build_slots_view(panel_id)
    return embed, view

async def refresh_panel_message(interaction: discord.Interaction, panel_id: int):
    try:
        embed, view = await render_panel(panel_id)
        await interaction.message.edit(embed=embed, view=view)  # type: ignore
    except Exception:
        pass

# =========================================================
# Break Select (ephemeral)
# =========================================================
class BreakSelectView(discord.ui.View):
    def __init__(self, panel_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=120)
        self.panel_id = panel_id
        sel = discord.ui.Select(
            custom_id=f"breaksel:{panel_id}",
            placeholder="枠を選択（休憩ON/OFF）",
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

        # 公開パネルも更新
        await refresh_panel_message(interaction, self.panel_id)

# =========================================================
# Component router (slot/notify/break/del はここで処理)
#  ※公開パネルのボタンが「再起動後も動く」ようにするため
# =========================================================
@client.event
async def on_interaction(interaction: discord.Interaction):
    try:
        # slash command
        if interaction.type == discord.InteractionType.application_command:
            await tree._call(interaction)
            return

        # components
        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data or {}
        cid = data.get("custom_id") or ""
        if not isinstance(cid, str):
            return

        # setup は View callback に任せる（ここでは触らない）
        if cid.startswith("setup:") or cid.startswith("breaksel:"):
            return

        # ---- slot reserve/cancel ----
        if cid.startswith("slot:"):
            slot_id = int(cid.split(":", 1)[1])
            await interaction.response.defer(ephemeral=True)

            sres = await db_to_thread(lambda: db_get_slot(slot_id))
            if not sres.data:
                await interaction.followup.send("❌ その枠が見つからない", ephemeral=True)
                return
            slot = sres.data[0]
            panel_id = int(slot["panel_id"])

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

            await refresh_panel_message(interaction, panel_id)
            return

        # ---- notify toggle ----
        if cid.startswith("notify:"):
            panel_id = int(cid.split(":", 1)[1])

            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            pres = await db_to_thread(lambda: sb.table("panels").select("notify_enabled").eq("id", panel_id).limit(1).execute())
            cur = True
            if pres.data and pres.data[0].get("notify_enabled") is not None:
                cur = bool(pres.data[0]["notify_enabled"])

            # notify_enabled列が無い場合もあるので保険
            try:
                await db_to_thread(lambda: db_update_panel(panel_id, {"notify_enabled": (not cur)}))
            except Exception as e:
                await interaction.followup.send(f"❌ notify_enabled更新失敗: {e}", ephemeral=True)
                return

            await interaction.followup.send(f"✅ 通知を {'ON' if (not cur) else 'OFF'} にした", ephemeral=True)
            await refresh_panel_message(interaction, panel_id)
            return

        # ---- break menu ----
        if cid.startswith("break:"):
            panel_id = int(cid.split(":", 1)[1])

            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            sres = await db_to_thread(lambda: db_get_slots(panel_id))
            slots = sres.data or []
            opts: list[discord.SelectOption] = []
            for s in slots[:25]:
                t = s.get("slot_time") or "??:??"
                is_break = bool(s.get("is_break", False))
                mark = "⚪" if is_break else "🟢"
                opts.append(discord.SelectOption(label=f"{mark} {t}", value=str(s["id"])))

            if not opts:
                await interaction.followup.send("❌ 枠がない", ephemeral=True)
                return

            await interaction.followup.send("休憩にする/戻す枠を選んでね👇", view=BreakSelectView(panel_id, opts), ephemeral=True)
            return

        # ---- delete panel ----
        if cid.startswith("del:"):
            panel_id = int(cid.split(":", 1)[1])

            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

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
                await interaction.message.delete()  # type: ignore
            except Exception:
                pass

            await interaction.followup.send("✅ パネルを削除した", ephemeral=True)
            return

    except Exception:
        # 何があってもbotが落ちないように
        return

# =========================================================
# COMMANDS
# =========================================================
@tree.command(name="setup", description="募集パネルを作る（自分だけ見える設定画面）")
async def setup(interaction: discord.Interaction):
    key = dkey(interaction)
    draft[key] = {
        "step": 1,
        "day_key": "today",  # 初期=今日（選ばなくてOK）
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
    }
    st = draft[key]
    await interaction.response.send_message(
        "ボタン/セレクトで設定してね👇（※この画面は自分だけ見える）",
        embed=build_setup_embed(st),
        view=SetupStep1View(st),
        ephemeral=True
    )

@tree.command(name="manager_role", description="管理ロールを設定/確認/解除（管理者のみ）")
@app_commands.describe(role="設定したいロール（未指定なら確認）", clear="trueで解除")
async def manager_role(interaction: discord.Interaction, role: discord.Role | None = None, clear: bool = False):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ サーバー管理者のみ実行できます", ephemeral=True)
        return

    gid_int = int(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)

    if clear:
        try:
            await db_to_thread(lambda: db_set_manager_role_id(gid_int, None))
        except Exception as e:
            await interaction.followup.send(f"❌ 解除失敗: {e}", ephemeral=True)
            return
        await interaction.followup.send("✅ 管理ロールを解除した", ephemeral=True)
        return

    if role is None:
        rid = await db_to_thread(lambda: db_get_manager_role_id(gid_int))
        if not rid:
            await interaction.followup.send("ℹ️ 管理ロールは未設定です（/manager_role で設定 / clear:true で解除）", ephemeral=True)
        else:
            await interaction.followup.send(f"ℹ️ 現在の管理ロール: <@&{int(rid)}>", ephemeral=True)
        return

    try:
        await db_to_thread(lambda: db_set_manager_role_id(gid_int, int(role.id)))
    except Exception as e:
        await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ 管理ロールを {role.mention} に設定した", ephemeral=True)

# =========================================================
# 3分前通知（バックグラウンドループ）
# =========================================================
async def reminder_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            now = datetime.now(UTC)
            window_end = now + timedelta(minutes=3)

            # notify_enabled が無い環境でも落ちないように保険
            try:
                pres = await db_to_thread(
                    lambda: sb.table("panels")
                        .select("id,notify_channel_id,interval_minutes,notify_enabled")
                        .eq("notify_enabled", True)
                        .execute()
                )
                panels = pres.data or []
            except Exception:
                pres = await db_to_thread(
                    lambda: sb.table("panels")
                        .select("id,notify_channel_id,interval_minutes")
                        .execute()
                )
                panels = pres.data or []

            for p in panels[:80]:
                panel_id = int(p["id"])
                notify_channel_id = p.get("notify_channel_id")
                if not notify_channel_id:
                    continue

                # 3分以内に開始する候補（DB側で not-null 指定に依存しない）
                sres = await db_to_thread(
                    lambda: sb.table("slots")
                        .select("*")
                        .eq("panel_id", panel_id)
                        .eq("notified", False)
                        .gte("start_at", now.isoformat())
                        .lte("start_at", window_end.isoformat())
                        .order("start_at")
                        .execute()
                )
                candidates = sres.data or []
                # reserved_by があるものだけ
                candidates = [s for s in candidates if s.get("reserved_by") and not bool(s.get("is_break", False))]
                if not candidates:
                    continue

                interval = int(p.get("interval_minutes") or 30)

                # 候補ごとに「連続枠」を未来まで伸ばして1回で通知
                for s in candidates:
                    if s.get("notified"):
                        continue

                    user_id = str(s["reserved_by"])
                    st = parse_iso(s["start_at"])
                    en = parse_iso(s["end_at"])

                    # 未来の同ユーザー枠を少し先まで取得して連続判定
                    fut = await db_to_thread(
                        lambda: sb.table("slots")
                            .select("*")
                            .eq("panel_id", panel_id)
                            .eq("reserved_by", user_id)
                            .order("start_at")
                            .gte("start_at", st.isoformat())
                            .limit(30)
                            .execute()
                    )
                    future = fut.data or []

                    # 連続を伸ばす
                    chain_ids = []
                    last_start = st
                    for t in future:
                        if bool(t.get("is_break", False)):
                            break
                        if t.get("reserved_by") != user_id:
                            break
                        ts = parse_iso(t["start_at"])
                        if ts == last_start or ts == last_start + timedelta(minutes=interval):
                            chain_ids.append(int(t["id"]))
                            last_start = ts
                            en = parse_iso(t["end_at"])
                        else:
                            break

                    # 通知送信
                    ch = client.get_channel(int(notify_channel_id))
                    if ch is None:
                        continue

                    msg = f"⏰ {st.astimezone(JST).strftime('%H:%M')}〜{en.astimezone(JST).strftime('%H:%M')} の枠です <@{user_id}>"
                    try:
                        await ch.send(msg)
                    except Exception:
                        continue

                    # notified=True
                    for _id in set(chain_ids):
                        try:
                            await db_to_thread(lambda _id=_id: db_update_slot(_id, {"notified": True}))
                        except Exception:
                            pass

        except Exception:
            pass

        await asyncio.sleep(30)

# =========================================================
# READY
# =========================================================
@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

    # ループ開始（多重起動防止）
    if not getattr(client, "_reminder_started", False):
        client._reminder_started = True
        asyncio.create_task(reminder_loop())

async def main():
    await asyncio.sleep(5)  # 429避け
    await client.start(TOKEN)

asyncio.run(main())