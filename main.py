import os
import re
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

# setup中の一時状態（メモリ）: (guild_id, user_id) -> state
draft: dict[tuple[str, str], dict] = {}

# notify_enabled 列が無いときの暫定保存（panel_id -> bool）
_notify_cache: dict[int, bool] = {}

# パネルの「今どのページ見てたか」キャッシュ（panel_id -> page）
_panel_page_cache: dict[int, int] = {}

# panel編集の連打抑制
_panel_locks: dict[int, asyncio.Lock] = {}
_panel_last_edit: dict[int, float] = {}

# =========================================================
# Helpers
# =========================================================
PER_PAGE = 20  # 1ページの枠ボタン数（20が安全：管理ボタン5で合計25）


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
    lock = _panel_locks.get(panel_id)
    if lock is None:
        lock = asyncio.Lock()
        _panel_locks[panel_id] = lock
    return lock


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _extract_missing_column(exc: Exception) -> str | None:
    msg = ""
    if hasattr(exc, "message"):
        try:
            msg = str(getattr(exc, "message"))
        except Exception:
            msg = ""
    if not msg and getattr(exc, "args", None):
        a0 = exc.args[0]
        if isinstance(a0, dict):
            msg = str(a0.get("message") or "")
        else:
            msg = str(a0)

    m = re.search(r"Could not find the '([^']+)' column", msg)
    return m.group(1) if m else None


# =========================================================
# DB helpers (safe)
# =========================================================
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


def db_get_slots(panel_id: int):
    return sb.table("slots").select("*").eq("panel_id", panel_id).order("start_at").execute()


def db_get_slot(slot_id: int):
    return sb.table("slots").select("*").eq("id", slot_id).limit(1).execute()


def db_delete_slots(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()


def db_delete_panel(guild_id: str, day_key: str):
    return sb.table("panels").delete().eq("guild_id", guild_id).eq("day_key", day_key).execute()


def db_set_manager_role_id(guild_id: str, role_id: int | None):
    row = {"guild_id": guild_id, "manager_role_id": role_id}
    return sb.table("guild_settings").upsert(row, on_conflict="guild_id").execute()


def db_get_manager_role_id(guild_id: str):
    res = sb.table("guild_settings").select("manager_role_id").eq("guild_id", guild_id).limit(1).execute()
    if res.data:
        return res.data[0].get("manager_role_id")
    return None


def db_upsert_panel_safe(row: dict):
    payload = dict(row)
    removed = set()
    for _ in range(12):
        try:
            return sb.table("panels").upsert(payload, on_conflict="guild_id,day_key").execute()
        except Exception as e:
            col = _extract_missing_column(e)
            if col and col in payload and col not in removed:
                removed.add(col)
                payload.pop(col, None)
                continue
            raise
    return sb.table("panels").upsert(payload, on_conflict="guild_id,day_key").execute()


def db_update_panel_safe(panel_id: int, patch: dict):
    payload = dict(patch)
    removed = set()
    for _ in range(12):
        try:
            return sb.table("panels").update(payload).eq("id", panel_id).execute()
        except Exception as e:
            col = _extract_missing_column(e)
            if col and col in payload and col not in removed:
                removed.add(col)
                payload.pop(col, None)
                continue
            raise
    return sb.table("panels").update(payload).eq("id", panel_id).execute()


def db_insert_slots_safe(rows: list[dict]):
    payload_rows = [dict(r) for r in rows]
    try:
        return sb.table("slots").insert(payload_rows).execute()
    except Exception as e:
        col = _extract_missing_column(e)
        if not col:
            raise
        for r in payload_rows:
            r.pop(col, None)
        return sb.table("slots").insert(payload_rows).execute()


def db_update_slot_safe(slot_id: int, patch: dict):
    payload = dict(patch)
    removed = set()
    for _ in range(12):
        try:
            return sb.table("slots").update(payload).eq("id", slot_id).execute()
        except Exception as e:
            col = _extract_missing_column(e)
            if col and col in payload and col not in removed:
                removed.add(col)
                payload.pop(col, None)
                continue
            raise
    return sb.table("slots").update(payload).eq("id", slot_id).execute()


async def is_manager(interaction: discord.Interaction) -> bool:
    if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
        return True
    gid = str(interaction.guild_id)
    rid = await db_to_thread(lambda: db_get_manager_role_id(gid))
    if not rid:
        return False
    if isinstance(interaction.user, discord.Member):
        return any(r.id == int(rid) for r in interaction.user.roles)
    return False


# =========================================================
# Setup UI (ephemeral)
# =========================================================
def build_setup_embed(st: dict) -> discord.Embed:
    step = int(st.get("step") or 1)
    e = discord.Embed(title=f"募集パネル作成ウィザード（{step}/2）", color=0x5865F2)

    day_key = st.get("day_key", "today")
    e.add_field(name="日付", value=("今日" if day_key == "today" else "明日"), inline=True)

    start = hm_text(st.get("start_h"), st.get("start_m"))
    end = hm_text(st.get("end_h"), st.get("end_m"))
    e.add_field(name="開始", value=(start or "未選択"), inline=True)
    e.add_field(name="終了", value=(end or "未選択"), inline=True)

    if step >= 2:
        interval = st.get("interval_minutes")
        e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)

        title = st.get("title") or "無題"
        e.add_field(name="タイトル", value=title, inline=False)

        notify_id = st.get("notify_channel_id")
        notify_label = st.get("notify_channel_label")
        show = f"<#{notify_id}>" if notify_id else "未選択=このチャンネル"
        if notify_label:
            show = f"{notify_label} / {show}"
        e.add_field(name="通知チャンネル（3分前通知）", value=show, inline=False)

        everyone = bool(st.get("mention_everyone", False))
        e.add_field(name="@everyone（作成時1回）", value=("ON" if everyone else "OFF"), inline=True)

        e.set_footer(text="設定したら「作成」で公開パネルを投稿")
    else:
        e.set_footer(text="開始/終了を選んで「次へ」")

    return e


def _opt_nums(n: int, step: int = 1) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=f"{i:02d}", value=str(i)) for i in range(0, n, step)]


def _set_defaults(options: list[discord.SelectOption], selected_value: int | None) -> list[discord.SelectOption]:
    if selected_value is None:
        return options
    for o in options:
        if o.value == str(selected_value):
            o.default = True
    return options


class NoopButton(discord.ui.Button):
    async def callback(self, interaction: discord.Interaction):
        return


class NoopSelect(discord.ui.Select):
    async def callback(self, interaction: discord.Interaction):
        return


class NoopChannelSelect(discord.ui.ChannelSelect):
    async def callback(self, interaction: discord.Interaction):
        return


class TitleModal(discord.ui.Modal):
    def __init__(self, st: dict):
        super().__init__(title="タイトル入力", timeout=300, custom_id="setup:titlemodal")
        self.st = st
        self.name = discord.ui.TextInput(
            label="タイトル",
            placeholder="例：今日の部屋管理",
            max_length=50,
            required=False,
        )
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        try:
            await interaction.response.edit_message(embed=build_setup_embed(self.st), view=build_setup_view(self.st))
        except Exception:
            await interaction.response.send_message("✅ タイトルを保存したよ（次の操作で反映）", ephemeral=True)


def build_setup_view(st: dict) -> discord.ui.View:
    step = int(st.get("step") or 1)
    v = discord.ui.View(timeout=600)

    day_key = st.get("day_key", "today")
    v.add_item(NoopButton(
        label="今日",
        style=(discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary),
        custom_id="setup:day:today",
        row=0,
    ))
    v.add_item(NoopButton(
        label="明日",
        style=(discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary),
        custom_id="setup:day:tomorrow",
        row=0,
    ))

    if step == 1:
        v.add_item(NoopButton(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=0))

        sh, sm = st.get("start_h"), st.get("start_m")
        eh, em = st.get("end_h"), st.get("end_m")

        v.add_item(NoopSelect(
            custom_id="setup:start_h",
            placeholder=f"開始(時) 現在:{(f'{sh:02d}' if sh is not None else '--')}",
            options=_set_defaults(_opt_nums(24), sh),
            min_values=1, max_values=1,
            row=1,
        ))
        v.add_item(NoopSelect(
            custom_id="setup:start_m",
            placeholder=f"開始(分) 現在:{(f'{sm:02d}' if sm is not None else '--')}",
            options=_set_defaults(_opt_nums(60, step=5), sm),
            min_values=1, max_values=1,
            row=2,
        ))
        v.add_item(NoopSelect(
            custom_id="setup:end_h",
            placeholder=f"終了(時) 現在:{(f'{eh:02d}' if eh is not None else '--')}",
            options=_set_defaults(_opt_nums(25), eh),  # 0..24
            min_values=1, max_values=1,
            row=3,
        ))
        v.add_item(NoopSelect(
            custom_id="setup:end_m",
            placeholder=f"終了(分) 現在:{(f'{em:02d}' if em is not None else '--')}",
            options=_set_defaults(_opt_nums(60, step=5), em),
            min_values=1, max_values=1,
            row=4,
        ))
        return v

    interval = st.get("interval_minutes")
    v.add_item(NoopSelect(
        custom_id="setup:interval",
        placeholder=f"間隔（20/25/30） 現在:{(interval if interval else '--')}",
        options=[
            discord.SelectOption(label="20分", value="20", default=(interval == 20)),
            discord.SelectOption(label="25分", value="25", default=(interval == 25)),
            discord.SelectOption(label="30分", value="30", default=(interval == 30)),
        ],
        min_values=1, max_values=1,
        row=1,
    ))

    v.add_item(NoopButton(label="📝 タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=2))

    ev_on = bool(st.get("mention_everyone", False))
    ev_style = discord.ButtonStyle.danger if ev_on else discord.ButtonStyle.secondary
    ev_label = "@everyone ON" if ev_on else "@everyone OFF"
    v.add_item(NoopButton(label=ev_label, style=ev_style, custom_id="setup:everyone", row=2))

    notify_label = st.get("notify_channel_label")
    ph = "通知チャンネル（未選択=このチャンネル）"
    if notify_label:
        ph = f"通知チャンネル: {notify_label}"
    v.add_item(NoopChannelSelect(
        custom_id="setup:notify_channel",
        placeholder=ph,
        min_values=1, max_values=1,
        channel_types=[discord.ChannelType.text],
        row=3,
    ))

    v.add_item(NoopButton(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=4))
    v.add_item(NoopButton(label="作成（公開パネル投稿）", style=discord.ButtonStyle.success, custom_id="setup:create", row=4))
    return v


# =========================================================
# Panel Embed & View（ページ切替）
# =========================================================
def build_panel_embed(title: str, day_key: str, interval: int, slots: list[dict], page: int) -> discord.Embed:
    e = discord.Embed(title=title or "募集パネル", color=0x2B2D31)
    day_label = "今日" if day_key == "today" else "明日"

    total = len(slots)
    max_page = max(0, (total - 1) // PER_PAGE)
    page = clamp(page, 0, max_page)

    e.description = f"📅 {day_label}（JST） / interval {interval}min\nページ {page+1}/{max_page+1}（全{total}枠）"

    start_i = page * PER_PAGE
    end_i = min(total, start_i + PER_PAGE)
    part = slots[start_i:end_i]

    lines = []
    for s in part:
        t = s.get("slot_time") or "??:??"
        is_break = bool(s.get("is_break", False))
        reserved_by = s.get("reserved_by")
        if is_break:
            lines.append(f"⚪ {t} 休憩")
        elif reserved_by:
            lines.append(f"🔴 {t} <@{reserved_by}>")
        else:
            lines.append(f"🟢 {t}")

    e.add_field(name="表示中の枠", value="\n".join(lines) if lines else "なし", inline=False)
    e.set_footer(text="🟢空き / 🔴予約済み（本人は押すとキャンセル） / ⚪休憩（予約不可）")
    return e


def build_panel_view(panel_id: int, slots: list[dict], notify_enabled: bool, page: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)

    total = len(slots)
    max_page = max(0, (total - 1) // PER_PAGE)
    page = clamp(page, 0, max_page)

    start_i = page * PER_PAGE
    end_i = min(total, start_i + PER_PAGE)
    part = slots[start_i:end_i]

    for i, s in enumerate(part):
        t = s.get("slot_time") or "??:??"
        is_break = bool(s.get("is_break", False))
        reserved_by = s.get("reserved_by")

        if is_break:
            style = discord.ButtonStyle.secondary
            disabled = True
        elif reserved_by:
            style = discord.ButtonStyle.danger
            disabled = False
        else:
            style = discord.ButtonStyle.success
            disabled = False

        row = i // 5  # 0..3
        v.add_item(NoopButton(
            label=t,
            style=style,
            disabled=disabled,
            custom_id=f"slot:{panel_id}:{int(s['id'])}:{page}",
            row=row
        ))

    prev_disabled = (page <= 0)
    next_disabled = (page >= max_page)

    v.add_item(NoopButton(
        label="◀ 前へ",
        style=discord.ButtonStyle.secondary,
        disabled=prev_disabled,
        custom_id=f"page:{panel_id}:{page-1}",
        row=4
    ))
    v.add_item(NoopButton(
        label="次へ ▶",
        style=discord.ButtonStyle.secondary,
        disabled=next_disabled,
        custom_id=f"page:{panel_id}:{page+1}",
        row=4
    ))

    n_label = "🔔 通知ON" if notify_enabled else "🔕 通知OFF"
    n_style = discord.ButtonStyle.success if notify_enabled else discord.ButtonStyle.secondary
    v.add_item(NoopButton(label=n_label, style=n_style, custom_id=f"notify:{panel_id}:{page}", row=4))
    v.add_item(NoopButton(label="🛠 休憩切替", style=discord.ButtonStyle.secondary, custom_id=f"break:{panel_id}:{page}", row=4))
    v.add_item(NoopButton(label="🗑 削除", style=discord.ButtonStyle.danger, custom_id=f"del:{panel_id}:{page}", row=4))

    return v


async def refresh_panel_message(message: discord.Message, panel_id: int, page: int | None = None):
    lock = ensure_lock(panel_id)
    async with lock:
        now_loop = asyncio.get_event_loop().time()
        last = _panel_last_edit.get(panel_id, 0.0)
        if now_loop - last < 1.0:
            await asyncio.sleep(1.0 - (now_loop - last))

        pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
        if not pres.data:
            return
        panel = pres.data[0]

        sres = await db_to_thread(lambda: db_get_slots(panel_id))
        slots = sres.data or []

        title = panel.get("title", "無題")
        day_key = panel.get("day_key", "today")
        interval = int(panel.get("interval_minutes") or 30)

        notify_enabled = True
        if "notify_enabled" in panel and panel.get("notify_enabled") is not None:
            notify_enabled = bool(panel.get("notify_enabled"))
        else:
            notify_enabled = bool(_notify_cache.get(panel_id, True))

        total = len(slots)
        max_page = max(0, (total - 1) // PER_PAGE)
        if page is None:
            page = _panel_page_cache.get(panel_id, 0)
        page = clamp(int(page), 0, max_page)
        _panel_page_cache[panel_id] = page

        try:
            await message.edit(
                embed=build_panel_embed(title, day_key, interval, slots, page),
                view=build_panel_view(panel_id, slots, notify_enabled, page),
            )
            _panel_last_edit[panel_id] = asyncio.get_event_loop().time()
        except Exception as e:
            print("⚠️ refresh_panel_message edit error:", repr(e))


async def refresh_panel_message_by_panel_id(
    panel_id: int,
    guild: discord.Guild | None,
    fallback_message: discord.Message | None = None,
    page: int | None = None
):
    if fallback_message is not None:
        try:
            await refresh_panel_message(fallback_message, panel_id, page=page)
            return
        except Exception as e:
            print("⚠️ refresh_panel_message_by_panel_id fallback error:", repr(e))

    pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
    if not pres.data:
        return
    panel = pres.data[0]

    channel_id = panel.get("channel_id")
    message_id = panel.get("panel_message_id")
    if not channel_id or not message_id or guild is None:
        return

    ch = guild.get_channel(int(channel_id))
    if ch is None:
        return

    try:
        msg = await ch.fetch_message(int(message_id))
    except Exception as e:
        print("⚠️ fetch panel message error:", repr(e))
        return

    await refresh_panel_message(msg, panel_id, page=page)


def build_break_select_view(panel_id: int, slots: list[dict], page: int) -> discord.ui.View:
    opts: list[discord.SelectOption] = []
    for s in slots[:25]:
        t = s.get("slot_time") or "??:??"
        is_break = bool(s.get("is_break", False))
        reserved_by = s.get("reserved_by")
        if reserved_by:
            desc = "予約あり（休憩不可）"
        elif is_break:
            desc = "休憩中（選ぶと解除）"
        else:
            desc = "空き（選ぶと休憩）"
        label = f"{'⚪ ' if is_break else ''}{t}"
        opts.append(discord.SelectOption(label=label, value=str(int(s["id"])), description=desc))

    v = discord.ui.View(timeout=120)
    v.add_item(NoopSelect(
        custom_id=f"breaksel:{panel_id}:{page}",
        placeholder="休憩にする/解除する枠を選択",
        options=opts,
        min_values=1, max_values=1,
        row=0,
    ))
    return v


# =========================================================
# Setup -> Create
# =========================================================
async def do_create_panel(interaction: discord.Interaction, st: dict):
    sh, sm = st.get("start_h"), st.get("start_m")
    eh, em = st.get("end_h"), st.get("end_m")
    interval = st.get("interval_minutes")

    if None in (sh, sm, eh, em) or not interval:
        await interaction.followup.send("❌ 開始/終了/間隔が未選択。/setup からやり直してね", ephemeral=True)
        return

    day_key = st.get("day_key", "today")
    title = st.get("title") or "無題"
    mention_everyone = bool(st.get("mention_everyone", False))
    notify_channel_id = st.get("notify_channel_id") or str(interaction.channel_id)

    base = datetime.now(JST).date()
    if day_key == "tomorrow":
        base = base + timedelta(days=1)

    start_dt = datetime(base.year, base.month, base.day, int(sh), int(sm), tzinfo=JST)

    if int(eh) == 24:
        if int(em) != 0:
            await interaction.followup.send("❌ 終了が24時の場合、分は00しか選べません", ephemeral=True)
            return
        next_day = base + timedelta(days=1)
        end_dt = datetime(next_day.year, next_day.month, next_day.day, 0, 0, tzinfo=JST)
    else:
        end_dt = datetime(base.year, base.month, base.day, int(eh), int(em), tzinfo=JST)

    if end_dt <= start_dt:
        end_dt = end_dt + timedelta(days=1)

    row = {
        "guild_id": str(interaction.guild_id),
        "channel_id": str(interaction.channel_id),
        "day_key": day_key,
        "title": title,
        "interval_minutes": int(interval),
        "notify_channel_id": str(notify_channel_id),
        "mention_everyone": bool(mention_everyone),
        "notify_enabled": True,
        "created_by": str(interaction.user.id),
        "created_at": datetime.now(UTC).isoformat(),
    }

    try:
        pres = await db_to_thread(lambda: db_upsert_panel_safe(row))
    except Exception as e:
        await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
        return

    panel = pres.data[0] if getattr(pres, "data", None) else None
    if not panel:
        got = await db_to_thread(lambda: db_get_panel(str(interaction.guild_id), day_key))
        panel = got.data[0] if got.data else None
    if not panel:
        await interaction.followup.send("❌ panels 保存後に取得できない。DBを確認してね", ephemeral=True)
        return

    panel_id = int(panel["id"])
    _notify_cache.setdefault(panel_id, True)
    _panel_page_cache[panel_id] = 0

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
            "slot_time": cur.strftime("%H:%M"),
            "is_break": False,
            "notified": False,
            "reserved_by": None,
        })
        cur += timedelta(minutes=int(interval))

    try:
        await db_to_thread(lambda: db_insert_slots_safe(slot_rows))
    except Exception as e:
        await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)
        return

    sres = await db_to_thread(lambda: db_get_slots(panel_id))
    slots = sres.data or []
    if not slots:
        await interaction.followup.send("❌ slots が作れなかった（slots列/制約を確認）", ephemeral=True)
        return

    old_mid = panel.get("panel_message_id")
    notify_enabled = True
    if panel.get("notify_enabled") is not None:
        notify_enabled = bool(panel.get("notify_enabled"))
    else:
        notify_enabled = bool(_notify_cache.get(panel_id, True))

    embed = build_panel_embed(title, day_key, int(interval), slots, page=0)
    view = build_panel_view(panel_id, slots, notify_enabled, page=0)

    msg = None
    if old_mid:
        try:
            msg = await interaction.channel.fetch_message(int(old_mid))
            await msg.edit(content=None, embed=embed, view=view)
        except Exception:
            msg = None

    if msg is None:
        msg = await interaction.channel.send(
            content=f"📅 **{title}**（{'今日' if day_key == 'today' else '明日'}） / interval {interval}min\n下のボタンで予約してね👇",
            embed=embed,
            view=view,
        )

    try:
        await db_to_thread(lambda: db_update_panel_safe(panel_id, {"panel_message_id": str(msg.id)}))
    except Exception:
        pass

    if mention_everyone:
        try:
            await interaction.channel.send("@everyone 募集を開始しました！")
        except Exception:
            pass
        try:
            await db_to_thread(lambda: db_update_panel_safe(panel_id, {"mention_everyone": False}))
        except Exception:
            pass
        st["mention_everyone"] = False

    await interaction.followup.send("✅ 保存して、公開パネルを投稿した！", ephemeral=True)


# =========================================================
# COMMANDS
# =========================================================
@tree.command(name="setup", description="募集パネルを作る（自分だけ見える設定画面）")
async def setup(interaction: discord.Interaction):
    key = dkey(interaction)
    draft[key] = {
        "step": 1,
        "day_key": "today",
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
        "notify_channel_label": None,
    }
    st = draft[key]
    await interaction.response.send_message(
        "設定して進めてね（※この画面は自分だけ見える）",
        embed=build_setup_embed(st),
        view=build_setup_view(st),
        ephemeral=True,
    )


@tree.command(name="manager_role", description="管理ロールを設定/解除（管理者のみ）")
@app_commands.describe(role="管理ロール（解除したいときは未選択で実行）")
async def manager_role(interaction: discord.Interaction, role: discord.Role | None = None):
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
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
# INTERACTION ROUTER
# =========================================================
@client.event
async def on_interaction(interaction: discord.Interaction):
    try:
        if interaction.type == discord.InteractionType.application_command:
            await tree._call(interaction)
            return

        if interaction.type == discord.InteractionType.modal_submit:
            data = interaction.data or {}
            cid = data.get("custom_id") or ""
            if cid == "setup:titlemodal":
                key = dkey(interaction)
                st = draft.get(key)
                if not st:
                    await interaction.response.send_message("❌ 状態がありません。/setup をやり直してね", ephemeral=True)
                    return

                comps = data.get("components") or []
                val = ""
                try:
                    val = comps[0]["components"][0].get("value") or ""
                except Exception:
                    val = ""
                st["title"] = (val or "").strip() or "無題"

                try:
                    await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                except Exception:
                    await interaction.response.send_message("✅ タイトルを保存したよ（次の操作で反映）", ephemeral=True)
                return
            return

        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data or {}
        cid = data.get("custom_id") or ""
        values = data.get("values") or []

        # -----------------------
        # Setup wizard
        # -----------------------
        if cid.startswith("setup:"):
            key = dkey(interaction)
            st = draft.get(key)
            if not st:
                await interaction.response.send_message("❌ 状態がありません。/setup をやり直してね", ephemeral=True)
                return

            if cid == "setup:day:today":
                st["day_key"] = "today"
            elif cid == "setup:day:tomorrow":
                st["day_key"] = "tomorrow"
            elif cid == "setup:start_h" and values:
                st["start_h"] = int(values[0])
            elif cid == "setup:start_m" and values:
                st["start_m"] = int(values[0])
            elif cid == "setup:end_h" and values:
                st["end_h"] = int(values[0])
            elif cid == "setup:end_m" and values:
                st["end_m"] = int(values[0])
            elif cid == "setup:interval" and values:
                st["interval_minutes"] = int(values[0])
            elif cid == "setup:title":
                await interaction.response.send_modal(TitleModal(st))
                return
            elif cid == "setup:everyone":
                st["mention_everyone"] = not bool(st.get("mention_everyone", False))
            elif cid == "setup:notify_channel" and values:
                ch_id = str(values[0])
                st["notify_channel_id"] = ch_id
                label = None
                try:
                    if interaction.guild:
                        ch = interaction.guild.get_channel(int(ch_id))
                        if ch:
                            label = f"#{ch.name}"
                except Exception:
                    label = None
                st["notify_channel_label"] = label
            elif cid == "setup:next":
                sh, sm = st.get("start_h"), st.get("start_m")
                eh, em = st.get("end_h"), st.get("end_m")
                if None in (sh, sm, eh, em):
                    await interaction.response.send_message("❌ まず開始/終了を全部選んでね", ephemeral=True)
                    return
                st["step"] = 2
            elif cid == "setup:back":
                st["step"] = 1
            elif cid == "setup:create":
                await interaction.response.defer(ephemeral=True)
                await do_create_panel(interaction, st)
                try:
                    done = discord.Embed(title="✅ 作成しました", description="公開パネルを投稿しました。", color=0x57F287)
                    await interaction.edit_original_response(embed=done, view=None, content=None)
                except Exception:
                    pass
                return

            try:
                await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
            except Exception:
                try:
                    await interaction.followup.send("⚠️ 更新できなかったので /setup を開き直してね", ephemeral=True)
                except Exception:
                    pass
            return

        # -----------------------
        # Page navigation
        # -----------------------
        if cid.startswith("page:"):
            parts = cid.split(":")
            if len(parts) < 3:
                return
            panel_id = int(parts[1])
            page = int(parts[2])

            _panel_page_cache[panel_id] = max(0, page)

            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception:
                pass

            await refresh_panel_message_by_panel_id(panel_id, interaction.guild, fallback_message=interaction.message, page=page)
            return

        # -----------------------
        # Slot reserve
        # -----------------------
        if cid.startswith("slot:"):
            parts = cid.split(":")
            if len(parts) < 3:
                return
            panel_id = int(parts[1])
            slot_id = int(parts[2])
            page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else _panel_page_cache.get(panel_id, 0)
            _panel_page_cache[panel_id] = page

            await interaction.response.defer(ephemeral=True)

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

            if reserved_by and str(reserved_by) != user_id:
                await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
                return

            if reserved_by and str(reserved_by) == user_id:
                await db_to_thread(lambda: db_update_slot_safe(slot_id, {"reserved_by": None, "notified": False}))
                await interaction.followup.send("✅ キャンセルしたよ", ephemeral=True)
            else:
                def work():
                    return (
                        sb.table("slots")
                        .update({"reserved_by": user_id, "notified": False})
                        .eq("id", slot_id)
                        .is_("reserved_by", None)
                        .execute()
                    )
                upd = await db_to_thread(work)
                if not upd.data:
                    await interaction.followup.send("❌ その枠はもう埋まっています", ephemeral=True)
                    return
                await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

            await refresh_panel_message_by_panel_id(panel_id, interaction.guild, fallback_message=interaction.message, page=page)
            return

        # -----------------------
        # Notify toggle
        # -----------------------
        if cid.startswith("notify:"):
            parts = cid.split(":")
            panel_id = int(parts[1])
            page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else _panel_page_cache.get(panel_id, 0)
            _panel_page_cache[panel_id] = page

            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            try:
                pres = await db_to_thread(
                    lambda: sb.table("panels").select("notify_enabled").eq("id", panel_id).limit(1).execute()
                )
                if pres.data and "notify_enabled" in pres.data[0]:
                    cur = bool(pres.data[0]["notify_enabled"])
                    await db_to_thread(lambda: db_update_panel_safe(panel_id, {"notify_enabled": (not cur)}))
                    new_val = not cur
                else:
                    cur = bool(_notify_cache.get(panel_id, True))
                    _notify_cache[panel_id] = (not cur)
                    new_val = not cur
            except Exception:
                cur = bool(_notify_cache.get(panel_id, True))
                _notify_cache[panel_id] = (not cur)
                new_val = not cur

            await interaction.followup.send(f"✅ 通知を {'ON' if new_val else 'OFF'} にした", ephemeral=True)
            await refresh_panel_message_by_panel_id(panel_id, interaction.guild, fallback_message=interaction.message, page=page)
            return

        # -----------------------
        # Break toggle
        # -----------------------
        if cid.startswith("break:"):
            parts = cid.split(":")
            panel_id = int(parts[1])
            page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else _panel_page_cache.get(panel_id, 0)
            _panel_page_cache[panel_id] = page

            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            sres = await db_to_thread(lambda: db_get_slots(panel_id))
            slots = sres.data or []
            if not slots:
                await interaction.followup.send("❌ 枠がない", ephemeral=True)
                return

            await interaction.followup.send("休憩にする/解除する枠を選んでね👇", view=build_break_select_view(panel_id, slots, page), ephemeral=True)
            return

        if cid.startswith("breaksel:"):
            parts = cid.split(":")
            panel_id = int(parts[1])
            page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else _panel_page_cache.get(panel_id, 0)
            _panel_page_cache[panel_id] = page

            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

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
            await db_to_thread(lambda: db_update_slot_safe(slot_id, {"is_break": (not now_break)}))
            await interaction.followup.send(f"✅ {'休憩にした' if (not now_break) else '休憩解除した'}", ephemeral=True)

            await refresh_panel_message_by_panel_id(panel_id, interaction.guild, fallback_message=None, page=page)
            return

        # -----------------------
        # Delete
        # -----------------------
        if cid.startswith("del:"):
            parts = cid.split(":")
            panel_id = int(parts[1])
            page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else _panel_page_cache.get(panel_id, 0)
            _panel_page_cache[panel_id] = page

            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            pres = await db_to_thread(
                lambda: sb.table("panels").select("guild_id,day_key").eq("id", panel_id).limit(1).execute()
            )
            if not pres.data:
                await interaction.followup.send("❌ panels が見つからない", ephemeral=True)
                return

            guild_id = pres.data[0].get("guild_id")
            day_key = pres.data[0].get("day_key")

            try:
                await db_to_thread(lambda: db_delete_slots(panel_id))
                if guild_id and day_key:
                    await db_to_thread(lambda: db_delete_panel(str(guild_id), str(day_key)))
            except Exception as e:
                await interaction.followup.send(f"❌ 削除失敗: {e}", ephemeral=True)
                return

            try:
                if interaction.message:
                    await interaction.message.delete()
            except Exception:
                pass

            await interaction.followup.send("✅ パネルを削除した", ephemeral=True)
            return

    except Exception as e:
        print("❌ on_interaction error:", repr(e))
        try:
            if interaction.type == discord.InteractionType.component and not interaction.response.is_done():
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

            try:
                pres = await db_to_thread(
                    lambda: sb.table("panels")
                    .select("id,channel_id,notify_channel_id,interval_minutes,notify_enabled")
                    .execute()
                )
                panels = pres.data or []
                has_notify_col = True
            except Exception as e:
                print("[reminder_loop] panels 5cols error:", repr(e))
                pres = await db_to_thread(
                    lambda: sb.table("panels")
                    .select("id,channel_id,notify_channel_id,interval_minutes")
                    .execute()
                )
                panels = pres.data or []
                has_notify_col = False

            for p in panels[:120]:
                try:
                    panel_id = int(p["id"])
                    notify_channel_id = p.get("notify_channel_id") or p.get("channel_id")
                    if not notify_channel_id:
                        continue

                    interval = int(p.get("interval_minutes") or 30)

                    if has_notify_col and p.get("notify_enabled") is not None:
                        enabled = bool(p["notify_enabled"])
                    else:
                        enabled = bool(_notify_cache.get(panel_id, True))
                    if not enabled:
                        continue

                    sres = await db_to_thread(
                        lambda: sb.table("slots")
                        .select("id,start_at,end_at,reserved_by,notified,is_break")
                        .eq("panel_id", panel_id)
                        .not_.is_("reserved_by", "null")
                        .eq("notified", False)
                        .eq("is_break", False)
                        .gte("start_at", now.isoformat())
                        .lte("start_at", window_end.isoformat())
                        .order("start_at")
                        .execute()
                    )
                    slots = sres.data or []
                    if not slots:
                        continue

                    used: set[int] = set()

                    ch = client.get_channel(int(notify_channel_id))
                    if ch is None:
                        print(f"[reminder_loop] channel not cached: {notify_channel_id}")
                        continue

                    for i, s in enumerate(slots):
                        sid = int(s["id"])
                        if sid in used:
                            continue

                        user_id = str(s["reserved_by"])
                        st_dt = parse_iso(s["start_at"])
                        en_dt = parse_iso(s["end_at"])

                        group = [s]
                        used.add(sid)

                        last_start = st_dt
                        for t in slots[i + 1:]:
                            if str(t.get("reserved_by")) != user_id:
                                continue
                            ts = parse_iso(t["start_at"])
                            if ts == last_start + timedelta(minutes=interval):
                                group.append(t)
                                used.add(int(t["id"]))
                                last_start = ts
                                en_dt = parse_iso(t["end_at"])

                        msg = (
                            f"⏰ {st_dt.astimezone(JST).strftime('%H:%M')}〜"
                            f"{en_dt.astimezone(JST).strftime('%H:%M')} の枠です <@{user_id}>"
                        )

                        try:
                            await ch.send(msg)
                        except Exception as e:
                            print("[reminder_loop] send error:", repr(e))
                            continue

                        for g in group:
                            try:
                                await db_to_thread(
                                    lambda _id=int(g["id"]): db_update_slot_safe(_id, {"notified": True})
                                )
                            except Exception as e:
                                print("[reminder_loop] notified update error:", repr(e))

                except Exception as e:
                    print("[reminder_loop] panel error:", repr(e))

        except Exception as e:
            print("[reminder_loop] outer error:", repr(e))

        await asyncio.sleep(60)


# =========================================================
# READY / RUN
# =========================================================
@client.event
async def on_ready():
    if not getattr(client, "_synced", False):
        try:
            await tree.sync()
            client._synced = True
            print("✅ commands synced")
        except Exception as e:
            print("⚠️ sync error:", repr(e))

    print(f"✅ Logged in as {client.user}")

    task = getattr(client, "_reminder_task", None)
    if task is None or task.done():
        print("⏸ reminder_loop stopped for debug")


if __name__ == "__main__":
    client.run(TOKEN)