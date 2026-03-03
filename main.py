# main.py（完全コピペ版）
from __future__ import annotations

import os
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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
draft: dict[tuple[str, str], dict[str, Any]] = {}

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

def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

# =========================================================
# DB helpers
# =========================================================
def db_upsert_panel(row: dict):
    # panels に (guild_id, day_key) ユニーク前提
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

def db_update_panel(panel_id: int, patch: dict):
    return sb.table("panels").update(patch).eq("id", panel_id).execute()

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
    # サーバー管理者は常にOK
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
# Setup Embed / View
# =========================================================
def build_setup_embed(st: dict[str, Any]) -> discord.Embed:
    step = int(st.get("step", 1))
    day_key = st.get("day_key", "today")
    day_label = "今日" if day_key == "today" else "明日"

    start = hm_text(st.get("start_h"), st.get("start_m")) or "未選択"
    end = hm_text(st.get("end_h"), st.get("end_m")) or "未選択"

    interval = st.get("interval_minutes")
    interval_text = f"{interval}分" if interval else "未選択"

    title = st.get("title") or "無題"

    notify = st.get("notify_channel_id")
    notify_text = f"<#{notify}>" if notify else "未選択=このチャンネル"

    everyone = bool(st.get("mention_everyone", False))
    everyone_text = "ON" if everyone else "OFF"

    # 画像の「縦並び」っぽくする
    lines = [
        "募集パネル作成ウィザード",
        "ボタン/セレクトで設定して「作成」",
        "",
        f"Step\n{step}",
        f"日付\n{day_label}",
        f"開始\n{start}",
        f"終了\n{end}",
    ]
    if step >= 2:
        lines += [
            f"間隔\n{interval_text}",
            f"タイトル\n{title}",
            f"通知チャンネル（3分前通知）\n{notify_text}",
            "@everyone\n" + everyone_text,
        ]

    lines += ["", "Step1→「次へ」 / Step2→「作成」"]

    e = discord.Embed(description="\n".join(lines), color=0x5865F2)
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

def build_setup_view(st: dict[str, Any]) -> discord.ui.View:
    step = int(st.get("step", 1))
    v = discord.ui.View(timeout=600)

    if step == 1:
        day_key = st.get("day_key", "today")
        btn_today_style = discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary
        btn_tom_style = discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary

        # row0: 日付 + 次へ（ボタンは幅1、合計<=5）
        v.add_item(discord.ui.Button(label="今日", style=btn_today_style, custom_id="setup:day:today", row=0))
        v.add_item(discord.ui.Button(label="明日", style=btn_tom_style, custom_id="setup:day:tomorrow", row=0))
        v.add_item(discord.ui.Button(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=0))

        # row1-4: Selectは幅5なので「1行に1個」だけ
        sh = st.get("start_h")
        sm = st.get("start_m")
        eh = st.get("end_h")
        em = st.get("end_m")

        v.add_item(discord.ui.Select(
            custom_id="setup:start_h",
            placeholder=f"開始(時){'' if sh is None else f' = {int(sh):02d}'}",
            options=_set_defaults(_opt_nums(24), sh),
            row=1,
        ))
        v.add_item(discord.ui.Select(
            custom_id="setup:start_m",
            placeholder=f"開始(分){'' if sm is None else f' = {int(sm):02d}'}",
            options=_set_defaults(_opt_nums(60, step=5), sm),
            row=2,
        ))
        v.add_item(discord.ui.Select(
            custom_id="setup:end_h",
            placeholder=f"終了(時){'' if eh is None else f' = {int(eh):02d}'}",
            options=_set_defaults(_opt_nums(24), eh),
            row=3,
        ))
        v.add_item(discord.ui.Select(
            custom_id="setup:end_m",
            placeholder=f"終了(分){'' if em is None else f' = {int(em):02d}'}",
            options=_set_defaults(_opt_nums(60, step=5), em),
            row=4,
        ))
        return v

    # ===== Step2 =====
    interval = st.get("interval_minutes")
    title = st.get("title") or "無題"
    ev_on = bool(st.get("mention_everyone", False))

    # row0: interval select（幅5で単独）
    v.add_item(discord.ui.Select(
        custom_id="setup:interval",
        placeholder=("間隔（20/25/30）" if not interval else f"間隔 = {int(interval)}分"),
        options=[
            discord.SelectOption(label="20分", value="20", default=(interval == 20)),
            discord.SelectOption(label="25分", value="25", default=(interval == 25)),
            discord.SelectOption(label="30分", value="30", default=(interval == 30)),
        ],
        row=0
    ))

    # row1: title + everyone（ボタン幅1）
    v.add_item(discord.ui.Button(label=f"📝 タイトル入力（今: {title}）", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=1))
    v.add_item(discord.ui.Button(
        label=("@everyone ON" if ev_on else "@everyone OFF"),
        style=(discord.ButtonStyle.danger if ev_on else discord.ButtonStyle.secondary),
        custom_id="setup:everyone",
        row=1
    ))

    # row2: notify channel select（幅5で単独）
    notify_id = st.get("notify_channel_id")
    notify_ph = "通知チャンネル（未選択=このチャンネル）"
    if notify_id:
        notify_ph = f"通知チャンネル = <#{notify_id}>"

    v.add_item(discord.ui.ChannelSelect(
        custom_id="setup:notify_channel",
        placeholder=notify_ph,
        min_values=1, max_values=1,
        channel_types=[discord.ChannelType.text],
        row=2
    ))

    # row3: back + create
    v.add_item(discord.ui.Button(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=3))
    v.add_item(discord.ui.Button(label="作成（公開パネル投稿）", style=discord.ButtonStyle.success, custom_id="setup:create", row=3))

    return v

class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False)

    def __init__(self, st: dict[str, Any]):
        super().__init__(timeout=300)
        self.st = st

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        # ※モーダルからは元メッセージを直接editできないので、通知だけ出す
        await interaction.response.send_message("✅ タイトルを反映したよ（画面は次の操作で更新される）", ephemeral=True)

# =========================================================
# Public Panel (Embed / View)
# =========================================================
def build_panel_embed(panel: dict[str, Any], slots: list[dict[str, Any]]) -> discord.Embed:
    title = panel.get("title", "無題")
    day_key = panel.get("day_key", "today")
    day_label = "今日" if day_key == "today" else "明日"
    interval = safe_int(panel.get("interval_minutes"), 30)

    e = discord.Embed(title="募集パネル", color=0x2B2D31)
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

def build_panel_view(panel_id: int, panel: dict[str, Any], slots: list[dict[str, Any]]) -> discord.ui.View:
    v = discord.ui.View(timeout=None)

    # slot buttons: 最大20（4行×5）
    for idx, s in enumerate(slots[:20]):
        row = idx // 5  # 0..3
        t = s.get("slot_time") or "??:??"
        is_break = bool(s.get("is_break", False))
        reserved_by = s.get("reserved_by")

        if is_break:
            style = discord.ButtonStyle.secondary
        elif reserved_by:
            style = discord.ButtonStyle.danger
        else:
            style = discord.ButtonStyle.success

        v.add_item(discord.ui.Button(
            label=t,
            style=style,
            custom_id=f"slot:{int(s['id'])}",
            row=row
        ))

    # 管理ボタン row4
    notify_enabled = True
    if panel.get("notify_enabled") is not None:
        notify_enabled = bool(panel["notify_enabled"])

    v.add_item(discord.ui.Button(
        label=("🔔 通知ON" if notify_enabled else "🔕 通知OFF"),
        style=(discord.ButtonStyle.success if notify_enabled else discord.ButtonStyle.secondary),
        custom_id=f"notify:{panel_id}",
        row=4
    ))
    v.add_item(discord.ui.Button(
        label="🛠 休憩切替（管理者/管理ロール）",
        style=discord.ButtonStyle.secondary,
        custom_id=f"break:{panel_id}",
        row=4
    ))
    v.add_item(discord.ui.Button(
        label="🗑 削除（管理者/管理ロール）",
        style=discord.ButtonStyle.danger,
        custom_id=f"del:{panel_id}",
        row=4
    ))
    return v

async def refresh_panel_message_by_panel_id(panel_id: int):
    pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
    if not pres.data:
        return
    panel = pres.data[0]
    msg_id = panel.get("panel_message_id")
    ch_id = panel.get("channel_id")
    if not msg_id or not ch_id:
        return

    sres = await db_to_thread(lambda: db_get_slots(panel_id))
    slots = sres.data or []

    channel = client.get_channel(int(ch_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(ch_id))
        except Exception:
            return

    try:
        msg = await channel.fetch_message(int(msg_id))
    except Exception:
        return

    try:
        await msg.edit(
            embed=build_panel_embed(panel, slots),
            view=build_panel_view(panel_id, panel, slots)
        )
    except Exception:
        return

# =========================================================
# COMPONENT HANDLER（再起動後もボタンが死なない方式）
# =========================================================
@client.event
async def on_interaction(interaction: discord.Interaction):
    try:
        # スラッシュコマンドは tree に渡す
        if interaction.type == discord.InteractionType.application_command:
            await tree._call(interaction)
            return

        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data or {}
        cid = str(data.get("custom_id") or "")
        values = data.get("values") or []

        # -------------------------
        # setup wizard
        # -------------------------
        if cid.startswith("setup:"):
            key = dkey(interaction)
            st = draft.get(key)
            if not st:
                # draft切れ
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ 期限切れ。もう一度 /setup してね", ephemeral=True)
                return

            # day
            if cid == "setup:day:today":
                st["day_key"] = "today"
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return
            if cid == "setup:day:tomorrow":
                st["day_key"] = "tomorrow"
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return

            # next/back
            if cid == "setup:next":
                st["step"] = 2
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return
            if cid == "setup:back":
                st["step"] = 1
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return

            # select updates
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

                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=build_setup_embed(st), view=build_setup_view(st))
                return

            # title modal
            if cid == "setup:title":
                if not interaction.response.is_done():
                    await interaction.response.send_modal(TitleModal(st))
                return

            # everyone toggle
            if cid == "setup:everyone":
                st["mention_everyone"] = not bool(st.get("mention_everyone", False))
                if not interaction.response.is_done():
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

                # ✅ notify_channel は「3分前通知用」
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

                # panels保存（start_at/end_at は触らず事故回避）
                row = {
                    "guild_id": str(interaction.guild_id),
                    "channel_id": str(interaction.channel_id),  # 公開パネル投稿先（ここ）
                    "day_key": day_key,
                    "title": title,
                    "interval_minutes": int(interval),
                    "notify_channel_id": str(notify_channel_id),  # ✅ 3分前通知先
                    "mention_everyone": bool(mention_everyone),
                    "created_by": str(interaction.user.id),
                    "created_at": datetime.now(UTC).isoformat(),

                    # 画面反映用
                    "start_h": int(sh), "start_m": int(sm),
                    "end_h": int(eh), "end_m": int(em),
                    "start_hm": start_hm,
                    "end_hm": end_hm,
                }

                # notify_enabled が無い環境でも落ちないように try
                try:
                    row["notify_enabled"] = True
                except Exception:
                    pass

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

                # slots作成（既存削除）
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
                sres = await db_to_thread(lambda: db_get_slots(panel_id))
                slots = sres.data or []

                msg = await ch.send(
                    content=f"📅 **{title}**（{'今日' if day_key=='today' else '明日'}） / interval {interval}min\n下のボタンで予約してね👇",
                    embed=build_panel_embed(panel, slots),
                    view=build_panel_view(panel_id, panel, slots),
                )

                # message_id保存（任意）
                try:
                    await db_to_thread(lambda: db_update_panel(panel_id, {"panel_message_id": str(msg.id)}))
                except Exception:
                    pass

                # 作成時 @everyone 1回だけ
                if mention_everyone:
                    try:
                        await ch.send("@everyone 募集を開始しました！")
                        # 次回以降OFFにする（DB側）
                        try:
                            await db_to_thread(lambda: db_update_panel(panel_id, {"mention_everyone": False}))
                        except Exception:
                            pass
                    except Exception:
                        pass

                await interaction.followup.send("✅ 保存して、公開パネルを投稿した！", ephemeral=True)
                return

            # ここまで来たらACKだけ（念のため）
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return

        # -------------------------
        # slot reserve / cancel
        # -------------------------
        if cid.startswith("slot:"):
            slot_id = safe_int(cid.split(":", 1)[1])
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

            # 他人予約は触れない
            if reserved_by and reserved_by != user_id:
                await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
                return

            # 自分ならキャンセル
            if reserved_by == user_id:
                # 競合対策: reserved_by=user_id のときだけ解除
                try:
                    await db_to_thread(lambda: sb.table("slots")
                        .update({"reserved_by": None, "reserver_user_id": None, "reserver_name": None, "reserved_at": None, "notified": False})
                        .eq("id", slot_id)
                        .eq("reserved_by", user_id)
                        .execute()
                    )
                except Exception as e:
                    await interaction.followup.send(f"❌ キャンセル失敗: {e}", ephemeral=True)
                    return

                await interaction.followup.send("✅ キャンセルしたよ", ephemeral=True)
            else:
                # 競合対策: reserved_by が null のときだけ予約
                patch = {
                    "reserved_by": user_id,
                    "reserver_user_id": int(user_id),
                    "reserver_name": interaction.user.display_name,
                    "reserved_at": datetime.now(UTC).isoformat(),
                    "notified": False,
                }
                try:
                    resu = await db_to_thread(lambda: sb.table("slots")
                        .update(patch)
                        .eq("id", slot_id)
                        .is_("reserved_by", "null")
                        .eq("is_break", False)
                        .execute()
                    )
                    if not (resu.data or []):
                        await interaction.followup.send("❌ その枠は先に取られたみたい", ephemeral=True)
                        return
                except Exception as e:
                    await interaction.followup.send(f"❌ 予約失敗: {e}", ephemeral=True)
                    return

                await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

            # パネル更新
            await refresh_panel_message_by_panel_id(panel_id)
            return

        # -------------------------
        # notify toggle
        # -------------------------
        if cid.startswith("notify:"):
            panel_id = safe_int(cid.split(":", 1)[1])
            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            # notify_enabled 列が無くても落ちない
            try:
                pres = await db_to_thread(lambda: sb.table("panels").select("notify_enabled").eq("id", panel_id).limit(1).execute())
                cur = True
                if pres.data and pres.data[0].get("notify_enabled") is not None:
                    cur = bool(pres.data[0]["notify_enabled"])
                await db_to_thread(lambda: db_update_panel(panel_id, {"notify_enabled": (not cur)}))
                await interaction.followup.send(f"✅ 通知を {'ON' if (not cur) else 'OFF'} にした", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ notify切替失敗（notify_enabled列が無い可能性）: {e}", ephemeral=True)
                return

            await refresh_panel_message_by_panel_id(panel_id)
            return

        # -------------------------
        # break open / break select
        # -------------------------
        if cid.startswith("break:"):
            panel_id = safe_int(cid.split(":", 1)[1])
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

            vv = discord.ui.View(timeout=120)
            vv.add_item(discord.ui.Select(
                custom_id=f"breaksel:{panel_id}",
                placeholder="休憩にする/戻す枠を選択",
                options=opts,
                min_values=1,
                max_values=1
            ))
            await interaction.followup.send("枠を選んでね👇", view=vv, ephemeral=True)
            return

        if cid.startswith("breaksel:"):
            panel_id = safe_int(cid.split(":", 1)[1])
            if not await is_manager(interaction):
                await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            if not values:
                await interaction.followup.send("❌ 選択が取れなかった", ephemeral=True)
                return
            slot_id = safe_int(values[0])

            sres = await db_to_thread(lambda: db_get_slot(slot_id))
            if not sres.data:
                await interaction.followup.send("❌ その枠が見つからない", ephemeral=True)
                return
            slot = sres.data[0]

            if slot.get("reserved_by"):
                await interaction.followup.send("❌ 予約済み枠は休憩にできない", ephemeral=True)
                return

            now_break = bool(slot.get("is_break", False))
            try:
                await db_to_thread(lambda: db_update_slot(slot_id, {"is_break": (not now_break)}))
            except Exception as e:
                await interaction.followup.send(f"❌ 休憩切替失敗: {e}", ephemeral=True)
                return

            await interaction.followup.send(f"✅ {'休憩にした' if (not now_break) else '休憩解除した'}", ephemeral=True)
            await refresh_panel_message_by_panel_id(panel_id)
            return

        # -------------------------
        # delete panel
        # -------------------------
        if cid.startswith("del:"):
            panel_id = safe_int(cid.split(":", 1)[1])
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

            # パネルメッセージも削除
            try:
                if interaction.message:
                    await interaction.message.delete()
            except Exception:
                pass

            await interaction.followup.send("✅ パネルを削除した", ephemeral=True)
            return

        # それ以外はACKだけ（「アプリが応答しませんでした」防止）
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    except Exception as e:
        # ここで落ちると「応答なし」になるので必ず握る
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ internal error: {e}", ephemeral=True)
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
        "day_key": "today",  # 初期は今日（選ばなくてOK）
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
    }
    st = draft[key]
    await interaction.response.send_message(
        "設定して進めてね（※この画面は自分だけ見える）",
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

# =========================================================
# 3分前通知（バックグラウンドループ）
# =========================================================
async def reminder_loop():
    await client.wait_until_ready()

    while not client.is_closed():
        try:
            now = datetime.now(UTC)
            window_end = now + timedelta(minutes=3)

            # notify_enabled列が無くても落ちないように取得を分岐
            try:
                pres = await db_to_thread(
                    lambda: sb.table("panels").select("id,notify_channel_id,interval_minutes,notify_enabled").execute()
                )
                panels = pres.data or []
            except Exception:
                pres = await db_to_thread(
                    lambda: sb.table("panels").select("id,notify_channel_id,interval_minutes").execute()
                )
                panels = pres.data or []

            for p in panels[:80]:
                # notify_enabledが存在してFalseならスキップ
                if p.get("notify_enabled") is not None and bool(p["notify_enabled"]) is False:
                    continue

                panel_id = int(p["id"])
                notify_channel_id = p.get("notify_channel_id")
                if not notify_channel_id:
                    continue

                interval = safe_int(p.get("interval_minutes"), 30)

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

                # 連続枠まとめ通知（同一ユーザー＆interval刻み）
                used: set[int] = set()

                for i, s in enumerate(slots):
                    sid = int(s["id"])
                    if sid in used:
                        continue

                    user_id = s["reserved_by"]
                    st_dt = parse_iso(s["start_at"])
                    en_dt = parse_iso(s["end_at"])

                    group = [s]
                    used.add(sid)

                    last_start = st_dt
                    for t in slots[i+1:]:
                        if t["reserved_by"] != user_id:
                            continue
                        ts = parse_iso(t["start_at"])
                        if ts == last_start + timedelta(minutes=interval):
                            group.append(t)
                            used.add(int(t["id"]))
                            last_start = ts
                            en_dt = parse_iso(t["end_at"])

                    # 通知送信
                    ch = client.get_channel(int(notify_channel_id))
                    if ch is None:
                        try:
                            ch = await client.fetch_channel(int(notify_channel_id))
                        except Exception:
                            continue

                    msg = f"⏰ {st_dt.astimezone(JST).strftime('%H:%M')}〜{en_dt.astimezone(JST).strftime('%H:%M')} の枠です <@{user_id}>"
                    try:
                        await ch.send(msg)
                    except Exception:
                        continue

                    # notified = true（まとめてループ更新）
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
# READY
# =========================================================
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")

    # tree.sync はAPIを叩くので多重実行しない
    if not getattr(client, "_synced", False):
        client._synced = True
        try:
            await tree.sync()
            print("✅ commands synced")
        except Exception as e:
            print("⚠️ sync failed:", e)

    # reminder loop 多重起動防止
    if not getattr(client, "_reminder_started", False):
        client._reminder_started = True
        asyncio.create_task(reminder_loop())

async def main():
    # 429避け（再デプロイ連打するとDiscord側でブロックされるので注意）
    await asyncio.sleep(10)
    await client.start(TOKEN)

asyncio.run(main())