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
# Helpers
# =========================================================
def parse_iso(dt_str: str) -> datetime:
    s = str(dt_str).replace("Z", "+00:00").replace(" ", "T")
    return datetime.fromisoformat(s)

def hm_text(h: int | None, m: int | None) -> str | None:
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"

async def db_to_thread(fn):
    return await asyncio.to_thread(fn)

def opt_nums(n: int, step: int = 1) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=f"{i:02d}", value=str(i)) for i in range(0, n, step)]

def set_defaults(options: list[discord.SelectOption], selected_value: int | None) -> list[discord.SelectOption]:
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

def db_get_panel_by_id(panel_id: int):
    return sb.table("panels").select("*").eq("id", panel_id).limit(1).execute()

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

# =========================================================
# Bot App
# =========================================================
class BotApp(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)

        # setup中の一時状態（メモリ）
        self.draft: dict[tuple[str, str], dict] = {}  # (guild_id, user_id)->state

        # 429対策：syncは1回だけ
        self._synced = False

        # 429対策：パネル更新をデバウンス
        self._pending_refresh: dict[int, asyncio.Task] = {}  # panel_id -> task

        # reminder多重起動防止
        self._reminder_started = False

    # -------------------------
    # Keys
    # -------------------------
    def dkey(self, interaction: discord.Interaction) -> tuple[str, str]:
        return (str(interaction.guild_id), str(interaction.user.id))

    # -------------------------
    # Setup embed/view
    # -------------------------
    def build_setup_embed(self, st: dict) -> discord.Embed:
        step = int(st.get("step", 1))
        e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)
        e.description = f"Step {step}"

        day_key = st.get("day_key", "today")
        e.add_field(name="日付", value=("今日" if day_key == "today" else "明日"), inline=True)

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

        e.set_footer(text="Step1→次へ / Step2→作成")
        return e

    def view_step1(self, st: dict) -> discord.ui.View:
        v = discord.ui.View(timeout=600)

        day_key = st.get("day_key", "today")
        v.add_item(discord.ui.Button(
            label="今日",
            style=(discord.ButtonStyle.primary if day_key == "today" else discord.ButtonStyle.secondary),
            custom_id="setup:day:today",
            row=0
        ))
        v.add_item(discord.ui.Button(
            label="明日",
            style=(discord.ButtonStyle.primary if day_key == "tomorrow" else discord.ButtonStyle.secondary),
            custom_id="setup:day:tomorrow",
            row=0
        ))
        v.add_item(discord.ui.Button(
            label="次へ",
            style=discord.ButtonStyle.success,
            custom_id="setup:next",
            row=0
        ))

        sh, sm = st.get("start_h"), st.get("start_m")
        eh, em = st.get("end_h"), st.get("end_m")

        v.add_item(discord.ui.Select(
            custom_id="setup:start_h",
            placeholder=(f"開始(時) {sh:02d}" if sh is not None else "開始(時)"),
            options=set_defaults(opt_nums(24), sh),
            row=1
        ))
        v.add_item(discord.ui.Select(
            custom_id="setup:start_m",
            placeholder=(f"開始(分) {sm:02d}" if sm is not None else "開始(分)"),
            options=set_defaults(opt_nums(60, step=5), sm),
            row=2
        ))
        v.add_item(discord.ui.Select(
            custom_id="setup:end_h",
            placeholder=(f"終了(時) {eh:02d}" if eh is not None else "終了(時)"),
            options=set_defaults(opt_nums(24), eh),
            row=3
        ))
        v.add_item(discord.ui.Select(
            custom_id="setup:end_m",
            placeholder=(f"終了(分) {em:02d}" if em is not None else "終了(分)"),
            options=set_defaults(opt_nums(60, step=5), em),
            row=4
        ))

        return v

    def view_step2(self, st: dict) -> discord.ui.View:
        v = discord.ui.View(timeout=600)

        interval = st.get("interval_minutes")
        v.add_item(discord.ui.Select(
            custom_id="setup:interval",
            placeholder=(f"間隔 {interval}分" if interval else "間隔（20/25/30）"),
            options=[
                discord.SelectOption(label="20分", value="20", default=(interval == 20)),
                discord.SelectOption(label="25分", value="25", default=(interval == 25)),
                discord.SelectOption(label="30分", value="30", default=(interval == 30)),
            ],
            row=0
        ))

        v.add_item(discord.ui.Button(label="📝 タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=1))

        ev_on = bool(st.get("mention_everyone", False))
        v.add_item(discord.ui.Button(
            label=("@everyone ON" if ev_on else "@everyone OFF"),
            style=(discord.ButtonStyle.danger if ev_on else discord.ButtonStyle.secondary),
            custom_id="setup:everyone",
            row=1
        ))

        v.add_item(discord.ui.ChannelSelect(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（未選択=このチャンネル）",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=2
        ))

        v.add_item(discord.ui.Button(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=3))
        v.add_item(discord.ui.Button(label="作成（公開投稿）", style=discord.ButtonStyle.success, custom_id="setup:create", row=3))
        return v

    # -------------------------
    # Title Modal
    # -------------------------
    class _TitleModal(discord.ui.Modal, title="タイトル入力"):
        name = discord.ui.TextInput(label="タイトル", placeholder="例：今日の部屋管理", max_length=50, required=False)

        def __init__(self, st: dict):
            super().__init__(timeout=300)
            self.st = st

        async def on_submit(self, interaction: discord.Interaction):
            self.st["title"] = (self.name.value or "").strip() or "無題"
            await interaction.response.send_message("✅ タイトルを反映したよ", ephemeral=True)

    # -------------------------
    # Panel render / debounce refresh
    # -------------------------
    def build_panel_embed(self, title: str, day_key: str, interval: int, slots: list[dict]) -> discord.Embed:
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

    async def build_slots_view(self, panel_id: int) -> discord.ui.View:
        res = await db_to_thread(lambda: db_get_slots(panel_id))
        slots = res.data or []

        v = discord.ui.View(timeout=None)
        # 20枠 (0..3行)
        for i, s in enumerate(slots[:20]):
            t = s.get("slot_time") or "??:??"
            is_break = bool(s.get("is_break", False))
            reserved_by = s.get("reserved_by")
            style = discord.ButtonStyle.success
            if is_break:
                style = discord.ButtonStyle.secondary
            elif reserved_by:
                style = discord.ButtonStyle.danger

            v.add_item(discord.ui.Button(
                label=t,
                style=style,
                custom_id=f"slot:{int(s['id'])}",
                row=(i // 5)
            ))

        # 管理ボタン row=4
        pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
        notify_enabled = True
        if pres.data and pres.data[0].get("notify_enabled") is not None:
            notify_enabled = bool(pres.data[0]["notify_enabled"])

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

    async def render_panel(self, panel_id: int) -> tuple[discord.Embed, discord.ui.View]:
        pres = await db_to_thread(lambda: db_get_panel_by_id(panel_id))
        if not pres.data:
            return discord.Embed(title="募集パネル", description="panel not found", color=0x2B2D31), discord.ui.View(timeout=None)
        panel = pres.data[0]

        sres = await db_to_thread(lambda: db_get_slots(panel_id))
        slots = sres.data or []

        title = panel.get("title", "無題")
        day_key = panel.get("day_key", "today")
        interval = int(panel.get("interval_minutes", 30))

        embed = self.build_panel_embed(title, day_key, interval, slots)
        view = await self.build_slots_view(panel_id)
        return embed, view

    async def schedule_refresh(self, panel_id: int, message: discord.Message):
        # 既に予約があるなら何もしない（連打でedit連発を防ぐ）
        if panel_id in self._pending_refresh:
            return

        async def worker():
            try:
                await asyncio.sleep(0.8)  # デバウンス
                embed, view = await self.render_panel(panel_id)
                await message.edit(embed=embed, view=view)
            except Exception:
                pass
            finally:
                self._pending_refresh.pop(panel_id, None)

        self._pending_refresh[panel_id] = asyncio.create_task(worker())

    # =========================================================
    # Interaction router
    # =========================================================
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            # スラッシュは tree に渡す（deferは各コマンド内）
            if interaction.type == discord.InteractionType.application_command:
                await self.tree._call(interaction)
                return

            if interaction.type != discord.InteractionType.component:
                return

            data = interaction.data or {}
            cid = data.get("custom_id") or ""
            if not isinstance(cid, str):
                return

            # -------------------------
            # Setup wizard (ephemeral)
            # -------------------------
            if cid.startswith("setup:"):
                key = self.dkey(interaction)
                st = self.draft.get(key)

                if not st:
                    if not interaction.response.is_done():
                        await interaction.response.send_message("状態がありません。/setup をやり直してね", ephemeral=True)
                    return

                # モーダル（defer禁止）
                if cid == "setup:title":
                    await interaction.response.send_modal(self._TitleModal(st))
                    return

                vals = data.get("values") or []

                if cid == "setup:day:today":
                    st["day_key"] = "today"
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step1(st))
                    return
                if cid == "setup:day:tomorrow":
                    st["day_key"] = "tomorrow"
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step1(st))
                    return

                if cid == "setup:start_h" and vals:
                    st["start_h"] = int(vals[0])
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step1(st))
                    return
                if cid == "setup:start_m" and vals:
                    st["start_m"] = int(vals[0])
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step1(st))
                    return
                if cid == "setup:end_h" and vals:
                    st["end_h"] = int(vals[0])
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step1(st))
                    return
                if cid == "setup:end_m" and vals:
                    st["end_m"] = int(vals[0])
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step1(st))
                    return

                if cid == "setup:next":
                    st["step"] = 2
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step2(st))
                    return
                if cid == "setup:back":
                    st["step"] = 1
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step1(st))
                    return

                if cid == "setup:interval" and vals:
                    st["interval_minutes"] = int(vals[0])
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step2(st))
                    return

                if cid == "setup:notify_channel" and vals:
                    st["notify_channel_id"] = str(vals[0])
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step2(st))
                    return

                if cid == "setup:everyone":
                    st["mention_everyone"] = not bool(st.get("mention_everyone", False))
                    await interaction.response.edit_message(embed=self.build_setup_embed(st), view=self.view_step2(st))
                    return

                if cid == "setup:create":
                    await interaction.response.defer(ephemeral=True)

                    sh, sm = st.get("start_h"), st.get("start_m")
                    eh, em = st.get("end_h"), st.get("end_m")
                    interval = st.get("interval_minutes")

                    if None in (sh, sm, eh, em) or not interval:
                        await interaction.followup.send("❌ 開始/終了/間隔が未選択。もう一度選んでね", ephemeral=True)
                        return

                    day_key = st.get("day_key", "today")
                    title = st.get("title") or "無題"
                    mention_everyone = bool(st.get("mention_everyone", False))
                    notify_channel_id = st.get("notify_channel_id") or str(interaction.channel_id)

                    base = datetime.now(JST).date()
                    if day_key == "tomorrow":
                        base = base + timedelta(days=1)

                    start_dt = datetime(base.year, base.month, base.day, int(sh), int(sm), tzinfo=JST)
                    end_dt = datetime(base.year, base.month, base.day, int(eh), int(em), tzinfo=JST)
                    if end_dt <= start_dt:
                        end_dt = end_dt + timedelta(days=1)

                    row = {
                        "guild_id": str(interaction.guild_id),
                        "channel_id": str(interaction.channel_id),   # 公開パネル投稿先
                        "day_key": day_key,
                        "title": title,
                        "interval_minutes": int(interval),
                        "notify_channel_id": str(notify_channel_id),
                        "mention_everyone": bool(mention_everyone),

                        "start_h": int(sh), "start_m": int(sm),
                        "end_h": int(eh), "end_m": int(em),
                        "start_hm": start_dt.strftime("%H:%M"),
                        "end_hm": end_dt.strftime("%H:%M"),

                        "created_by": str(interaction.user.id),
                        "created_at": datetime.now(UTC).isoformat(),
                    }

                    # notify_enabledが無い環境向けに保険
                    row2 = dict(row)
                    row2["notify_enabled"] = True

                    try:
                        pres = await db_to_thread(lambda: db_upsert_panel(row2))
                    except Exception as e:
                        es = repr(e)
                        if "PGRST204" in es and "notify_enabled" in es:
                            pres = await db_to_thread(lambda: db_upsert_panel(row))
                        else:
                            await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
                            return

                    panel = pres.data[0] if pres.data else None
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
                            "slot_time": cur.strftime("%H:%M"),
                            "is_break": False,
                            "notified": False,
                            "reserved_by": None,
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

                    embed, view = await self.render_panel(panel_id)
                    msg = await interaction.channel.send(
                        content=f"📅 **{title}**（{'今日' if day_key=='today' else '明日'}） / interval {interval}min\n下のボタンで予約してね👇",
                        embed=embed,
                        view=view
                    )

                    try:
                        await db_to_thread(lambda: db_update_panel(panel_id, {"panel_message_id": str(msg.id)}))
                    except Exception:
                        pass

                    if mention_everyone:
                        try:
                            await interaction.channel.send("@everyone 募集を開始しました！")
                            await db_to_thread(lambda: db_update_panel(panel_id, {"mention_everyone": False}))
                        except Exception:
                            pass

                    await interaction.followup.send("✅ 保存して、公開パネルを投稿した！", ephemeral=True)
                    return

                return  # setup: 未知は無視

            # -------------------------
            # Public panel buttons
            # -------------------------
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
                    await db_to_thread(lambda: db_update_slot(slot_id, {"reserved_by": None, "notified": False}))
                    await interaction.followup.send("✅ キャンセルしたよ", ephemeral=True)
                else:
                    await db_to_thread(lambda: db_update_slot(slot_id, {"reserved_by": user_id, "reserved_at": datetime.now(UTC).isoformat(), "notified": False}))
                    await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

                # ★ 429対策：即editせずデバウンス更新
                await self.schedule_refresh(panel_id, interaction.message)  # type: ignore
                return

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

                try:
                    await db_to_thread(lambda: db_update_panel(panel_id, {"notify_enabled": (not cur)}))
                except Exception as e:
                    await interaction.followup.send(f"❌ notify_enabled更新失敗: {e}", ephemeral=True)
                    return

                await interaction.followup.send(f"✅ 通知を {'ON' if (not cur) else 'OFF'} にした", ephemeral=True)
                await self.schedule_refresh(panel_id, interaction.message)  # type: ignore
                return

            # break/del は今のまま（必要なら次で入れる）

        except Exception:
            # 例外で落ちて再起動→ログイン連打→429 の悪循環を防ぐ
            return

    # =========================================================
    # Ready / sync / reminder
    # =========================================================
    async def on_ready(self):
        # syncは1回だけ（429対策）
        if not self._synced:
            try:
                await self.tree.sync()
            except Exception:
                pass
            self._synced = True

        print(f"✅ Logged in as {self.user}")

        if not self._reminder_started:
            self._reminder_started = True
            asyncio.create_task(self.reminder_loop())

    async def reminder_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                now = datetime.now(UTC)
                window_end = now + timedelta(minutes=3)

                # notify_enabled列が無い環境でも落ちないように2段構え
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
                    candidates = [s for s in candidates if s.get("reserved_by") and not bool(s.get("is_break", False))]
                    if not candidates:
                        continue

                    interval = int(p.get("interval_minutes") or 30)

                    for s in candidates:
                        user_id = str(s["reserved_by"])
                        st = parse_iso(s["start_at"])
                        en = parse_iso(s["end_at"])

                        # 連続枠まとめ（未来を見て伸ばす）
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

                        chain_ids = []
                        last_start = st
                        for t in future:
                            if bool(t.get("is_break", False)):
                                break
                            ts = parse_iso(t["start_at"])
                            if ts == last_start or ts == last_start + timedelta(minutes=interval):
                                chain_ids.append(int(t["id"]))
                                last_start = ts
                                en = parse_iso(t["end_at"])
                            else:
                                break

                        ch = self.get_channel(int(notify_channel_id))
                        if ch is None:
                            continue

                        msg = f"⏰ {st.astimezone(JST).strftime('%H:%M')}〜{en.astimezone(JST).strftime('%H:%M')} の枠です <@{user_id}>"
                        try:
                            await ch.send(msg)
                        except Exception:
                            continue

                        for _id in set(chain_ids):
                            try:
                                await db_to_thread(lambda _id=_id: db_update_slot(_id, {"notified": True}))
                            except Exception:
                                pass

            except Exception:
                pass

            await asyncio.sleep(30)

# =========================================================
# Commands registration
# =========================================================
bot = BotApp()

@bot.tree.command(name="setup", description="募集パネルを作る（自分だけ見える設定画面）")
async def setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    key = bot.dkey(interaction)
    bot.draft[key] = {
        "step": 1,
        "day_key": "today",
        "start_h": None, "start_m": None,
        "end_h": None, "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
    }
    st = bot.draft[key]

    await interaction.followup.send(
        "ボタン/セレクトで設定してね👇（※この画面は自分だけ見える）",
        embed=bot.build_setup_embed(st),
        view=bot.view_step1(st),
        ephemeral=True
    )

@bot.tree.command(name="manager_role", description="管理ロールを設定/確認/解除（管理者のみ）")
@app_commands.describe(role="設定したいロール（未指定なら確認）", clear="trueで解除")
async def manager_role(interaction: discord.Interaction, role: discord.Role | None = None, clear: bool = False):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ サーバー管理者のみ実行できます", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    gid_int = int(interaction.guild_id)

    if clear:
        await db_to_thread(lambda: db_set_manager_role_id(gid_int, None))
        await interaction.followup.send("✅ 管理ロールを解除した", ephemeral=True)
        return

    if role is None:
        rid = await db_to_thread(lambda: db_get_manager_role_id(gid_int))
        if not rid:
            await interaction.followup.send("ℹ️ 管理ロールは未設定です（/manager_role で設定 / clear:true で解除）", ephemeral=True)
        else:
            await interaction.followup.send(f"ℹ️ 現在の管理ロール: <@&{int(rid)}>", ephemeral=True)
        return

    await db_to_thread(lambda: db_set_manager_role_id(gid_int, int(role.id)))
    await interaction.followup.send(f"✅ 管理ロールを {role.mention} に設定した", ephemeral=True)

# =========================================================
# Runner with backoff (429対策：ログイン連打防止)
# =========================================================
async def run_bot_with_backoff():
    backoff = 5
    while True:
        try:
            await bot.start(TOKEN)
            return
        except discord.HTTPException as e:
            # ログインやAPIで429が返った場合、即死せずバックオフ
            if getattr(e, "status", None) == 429:
                print(f"⚠️ 429 Too Many Requests. backing off {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            raise
        except Exception as e:
            print(f"❌ fatal: {type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

async def main():
    # 起動直後のログイン連打回避
    await asyncio.sleep(5)
    await run_bot_with_backoff()

asyncio.run(main())