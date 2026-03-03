import os
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from supabase import create_client

# =========================
# ENV
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が未設定です")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_KEY が未設定です")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

JST = timezone(timedelta(hours=9))

# =========================
# Discord
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================
# In-memory state
# =========================
draft = {}  # (guild_id, user_id) -> dict


def dkey(interaction: discord.Interaction):
    return (str(interaction.guild_id), str(interaction.user.id))


async def db_to_thread(fn):
    return await asyncio.to_thread(fn)


# =========================
# DB helpers
# =========================
def db_upsert_panel(row: dict):
    # panels に (guild_id, day_key) のユニーク制約がある想定
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


def db_update_panel_message_id(panel_id: int, message_id: str):
    return sb.table("panels").update({"panel_message_id": message_id}).eq("id", panel_id).execute()


def db_delete_slots(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()


def db_insert_slots(rows: list[dict]):
    return sb.table("slots").insert(rows).execute()


def db_get_slots(panel_id: int):
    return sb.table("slots").select("*").eq("panel_id", panel_id).order("start_at").execute()


def db_get_slot(slot_id: int):
    return sb.table("slots").select("*").eq("id", slot_id).limit(1).execute()


def db_try_reserve(slot_id: int, user_id: str):
    # reserved_by が NULL の時だけ予約（競合防止）
    return (
        sb.table("slots")
        .update(
            {
                "reserved_by": user_id,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
                "reserver_user_id": int(user_id) if user_id.isdigit() else None,
            }
        )
        .eq("id", slot_id)
        .is_("reserved_by", "null")
        .execute()
    )


def db_try_cancel(slot_id: int, user_id: str):
    # 本人だけキャンセル可能
    return (
        sb.table("slots")
        .update(
            {
                "reserved_by": None,
                "reserved_at": None,
                "reserver_user_id": None,
                "reserver_name": None,
            }
        )
        .eq("id", slot_id)
        .eq("reserved_by", user_id)
        .execute()
    )


# =========================
# UI options
# =========================
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


def hm_from_state(st: dict, prefix: str):
    h = st.get(f"{prefix}_h")
    m = st.get(f"{prefix}_m")
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"


def day_label(day_key: str):
    return "今日" if day_key == "today" else "明日"


# =========================
# Embeds
# =========================
def build_setup_embed(st: dict):
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)

    day = st.get("day_key", "today")
    start = hm_from_state(st, "start")
    end = hm_from_state(st, "end")
    interval = st.get("interval_minutes")
    title = st.get("title") or "無題"
    notify = st.get("notify_channel_id")
    everyone = bool(st.get("mention_everyone", False))
    step = int(st.get("step", 1))

    e.add_field(name="Step", value=str(step), inline=False)
    e.add_field(name="日付", value=day_label(day), inline=True)
    e.add_field(name="開始", value=(start or "未選択"), inline=True)
    e.add_field(name="終了", value=(end or "未選択"), inline=True)
    e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)
    e.add_field(name="タイトル", value=title, inline=False)
    e.add_field(name="通知チャンネル", value=(f"<#{notify}>" if notify else "このチャンネル"), inline=False)
    e.add_field(name="@everyone", value=("ON" if everyone else "OFF"), inline=True)

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e


def fmt_slot_time(dt_utc_iso: str):
    dt = datetime.fromisoformat(str(dt_utc_iso).replace("Z", "+00:00"))
    return dt.astimezone(JST).strftime("%H:%M")


def build_panel_embed(panel: dict, slots: list[dict]):
    title = panel.get("title") or "募集パネル"
    day = panel.get("day_key", "today")
    interval = panel.get("interval_minutes")

    e = discord.Embed(title="募集パネル", color=0x2B2D31)
    e.description = f"📅 {day_label(day)}（JST） / interval {interval}min\n\n"

    lines = []
    for r in slots:
        t = fmt_slot_time(r["start_at"])
        is_break = bool(r.get("is_break", False))
        reserved_by = r.get("reserved_by")

        if is_break:
            lines.append(f"⚪ {t} 休憩")
        elif reserved_by:
            lines.append(f"🔴 {t} <@{reserved_by}>")
        else:
            lines.append(f"🟢 {t}")

    e.add_field(name="枠", value="\n".join(lines) if lines else "（枠なし）", inline=False)
    e.add_field(
        name="凡例",
        value="🟢空き / 🔴予約済み（本人は押すとキャンセル） / ⚪休憩（予約不可）",
        inline=False,
    )
    return e


# =========================
# Views
# =========================
class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(
        label="タイトル",
        placeholder="例：今日の部屋管理",
        max_length=50,
        required=False,
    )

    def __init__(self, st: dict):
        super().__init__(timeout=300)
        self.st = st

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"

        # 元のウィザードメッセージを更新（modal には message がない場合がある）
        ch_id = self.st.get("_wizard_channel_id")
        msg_id = self.st.get("_wizard_message_id")
        if ch_id and msg_id:
            ch = client.get_channel(int(ch_id))
            if ch:
                try:
                    msg = await ch.fetch_message(int(msg_id))
                    await msg.edit(embed=build_setup_embed(self.st), view=SetupView(self.st))
                except Exception:
                    pass

        await interaction.response.send_message("✅ タイトルを反映したよ", ephemeral=True)


class SetupView(discord.ui.View):
    def __init__(self, st: dict):
        super().__init__(timeout=None)
        self.st = st

        step = int(st.get("step", 1))
        day = st.get("day_key", "today")
        ev_on = bool(st.get("mention_everyone", False))
        cur_iv = st.get("interval_minutes")

        # ---- Step1: 日付 + 開始/終了 + 次へ
        if step == 1:
            # day buttons（選択中をハイライト）
            self.add_item(
                discord.ui.Button(
                    label="今日",
                    style=discord.ButtonStyle.primary if day == "today" else discord.ButtonStyle.secondary,
                    custom_id="setup:day:today",
                    row=0,
                )
            )
            self.add_item(
                discord.ui.Button(
                    label="明日",
                    style=discord.ButtonStyle.primary if day == "tomorrow" else discord.ButtonStyle.secondary,
                    custom_id="setup:day:tomorrow",
                    row=0,
                )
            )
            self.add_item(
                discord.ui.Button(
                    label="次へ",
                    style=discord.ButtonStyle.success,
                    custom_id="setup:next",
                    row=0,
                )
            )

            # start
            self.add_item(discord.ui.Select(custom_id="setup:start_h", placeholder="開始(時)", options=hour_options(), row=1))
            self.add_item(discord.ui.Select(custom_id="setup:start_m", placeholder="開始(分)", options=minute_options(5), row=2))

            # end
            self.add_item(discord.ui.Select(custom_id="setup:end_h", placeholder="終了(時)", options=hour_options(), row=3))
            self.add_item(discord.ui.Select(custom_id="setup:end_m", placeholder="終了(分)", options=minute_options(5), row=4))

        # ---- Step2: 間隔/タイトル/@everyone/通知ch + 戻る/作成
        else:
            self.add_item(
                discord.ui.Select(
                    custom_id="setup:interval",
                    placeholder=f"間隔（今: {cur_iv}分）" if cur_iv else "間隔（20/25/30）",
                    options=interval_options(),
                    row=0,
                )
            )
            self.add_item(
                discord.ui.Button(
                    label="タイトル入力",
                    style=discord.ButtonStyle.secondary,
                    custom_id="setup:title",
                    row=1,
                )
            )
            self.add_item(
                discord.ui.Button(
                    label="@everyone ON/OFF",
                    style=discord.ButtonStyle.danger if ev_on else discord.ButtonStyle.secondary,
                    custom_id="setup:everyone",
                    row=1,
                )
            )

            self.add_item(
                discord.ui.ChannelSelect(
                    custom_id="setup:notify_channel",
                    placeholder="通知チャンネル（未選択=このチャンネル）",
                    min_values=1,
                    max_values=1,
                    channel_types=[discord.ChannelType.text],
                    row=2,
                )
            )

            self.add_item(
                discord.ui.Button(
                    label="戻る",
                    style=discord.ButtonStyle.secondary,
                    custom_id="setup:back",
                    row=3,
                )
            )
            self.add_item(
                discord.ui.Button(
                    label="作成",
                    style=discord.ButtonStyle.success,
                    custom_id="setup:create",
                    row=3,
                )
            )


class SlotsView(discord.ui.View):
    def __init__(self, panel_id: int, slots: list[dict]):
        super().__init__(timeout=None)
        self.panel_id = panel_id

        # Discordのボタン上限25。パネル用は最大 20 に制限
        for r in slots[:20]:
            sid = int(r["id"])
            label = fmt_slot_time(r["start_at"])

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

            self.add_item(
                discord.ui.Button(
                    label=label,
                    style=style,
                    custom_id=f"slot:{sid}",
                    disabled=disabled,
                )
            )


# =========================
# Helper: refresh wizard message
# =========================
async def refresh_setup(interaction: discord.Interaction, st: dict):
    # component は必ずACKが必要。edit_messageでACKする。
    await interaction.response.edit_message(embed=build_setup_embed(st), view=SetupView(st))


# =========================
# Commands
# =========================
@tree.command(name="setup", description="募集パネルを作る（ウィザード）")
async def setup(interaction: discord.Interaction):
    key = dkey(interaction)
    draft[key] = {
        "step": 1,
        "day_key": "today",  # ✅ 初期は今日
        "start_h": None,
        "start_m": None,
        "end_h": None,
        "end_m": None,
        "interval_minutes": None,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": None,
        "_wizard_channel_id": str(interaction.channel_id),
        "_wizard_message_id": None,
    }
    st = draft[key]

    await interaction.response.send_message(
        "ボタン/セレクトで設定してね👇",
        embed=build_setup_embed(st),
        view=SetupView(st),
        ephemeral=False,
    )

    # 送信したメッセージIDを保存（Modalから編集する用）
    try:
        msg = await interaction.original_response()
        st["_wizard_message_id"] = str(msg.id)
    except Exception:
        pass


# =========================
# Interaction handler（componentを全部ここで処理）
# =========================
@client.event
async def on_interaction(interaction: discord.Interaction):
    try:
        # スラッシュコマンドは必ず tree に渡す
        if interaction.type == discord.InteractionType.application_command:
            await tree._call(interaction)
            return

        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data or {}
        cid = data.get("custom_id") or ""

        # ---- setup wizard ----
        if cid.startswith("setup:"):
            key = dkey(interaction)
            st = draft.get(key)
            if not st:
                # draftが消えてたら、最低限ACKして終わる
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ 先に /setup からやり直してね", ephemeral=True)
                return

            values = data.get("values") or []

            # day
            if cid == "setup:day:today":
                st["day_key"] = "today"
                await refresh_setup(interaction, st)
                return
            if cid == "setup:day:tomorrow":
                st["day_key"] = "tomorrow"
                await refresh_setup(interaction, st)
                return

            # step move
            if cid == "setup:next":
                # 開始/終了が未設定なら止める（応答はephemeral）
                if st.get("start_h") is None or st.get("start_m") is None or st.get("end_h") is None or st.get("end_m") is None:
                    await interaction.response.send_message("❌ 開始/終了を選んでから「次へ」してね", ephemeral=True)
                    return
                st["step"] = 2
                await refresh_setup(interaction, st)
                return

            if cid == "setup:back":
                st["step"] = 1
                await refresh_setup(interaction, st)
                return

            # start/end selects
            if cid == "setup:start_h" and values:
                st["start_h"] = int(values[0])
                await refresh_setup(interaction, st)
                return
            if cid == "setup:start_m" and values:
                st["start_m"] = int(values[0])
                await refresh_setup(interaction, st)
                return
            if cid == "setup:end_h" and values:
                st["end_h"] = int(values[0])
                await refresh_setup(interaction, st)
                return
            if cid == "setup:end_m" and values:
                st["end_m"] = int(values[0])
                await refresh_setup(interaction, st)
                return

            # interval
            if cid == "setup:interval" and values:
                st["interval_minutes"] = int(values[0])
                await refresh_setup(interaction, st)
                return

            # everyone toggle
            if cid == "setup:everyone":
                st["mention_everyone"] = not bool(st.get("mention_everyone", False))
                await refresh_setup(interaction, st)
                return

            # notify channel select
            if cid == "setup:notify_channel" and values:
                st["notify_channel_id"] = str(values[0])
                await refresh_setup(interaction, st)
                return

            # title modal
            if cid == "setup:title":
                # modalを出すのはここでACKになる
                await interaction.response.send_modal(TitleModal(st))
                return

            # create
            if cid == "setup:create":
                # まずACK（時間かかるのでdefer）
                await interaction.response.defer(ephemeral=True)

                # 必須チェック
                if st.get("start_h") is None or st.get("start_m") is None or st.get("end_h") is None or st.get("end_m") is None:
                    await interaction.followup.send("❌ 開始/終了が保存されてない。/setup からやり直してね", ephemeral=True)
                    return
                if st.get("interval_minutes") is None:
                    await interaction.followup.send("❌ 間隔（20/25/30）を選んでね", ephemeral=True)
                    return

                guild_id = str(interaction.guild_id)
                day_key = st.get("day_key", "today")
                title = st.get("title") or "無題"
                interval = int(st.get("interval_minutes"))
                notify_channel_id = st.get("notify_channel_id") or str(interaction.channel_id)
                mention_everyone = bool(st.get("mention_everyone", False))

                # start/end を JST の datetime にする
                base = datetime.now(JST).replace(second=0, microsecond=0)
                if day_key == "tomorrow":
                    base = base + timedelta(days=1)

                start_dt = base.replace(hour=int(st["start_h"]), minute=int(st["start_m"]))
                end_dt = base.replace(hour=int(st["end_h"]), minute=int(st["end_m"]))

                # 日跨ぎ対応（endがstartより前なら翌日にする）
                if end_dt <= start_dt:
                    end_dt = end_dt + timedelta(days=1)

                # panels 保存（upsert）
                panel_row = {
                    "guild_id": guild_id,
                    "day_key": day_key,
                    "channel_id": str(interaction.channel_id),
                    "title": title,
                    "interval_minutes": interval,
                    "notify_channel_id": notify_channel_id,
                    "mention_everyone": mention_everyone,
                    "start_h": int(st["start_h"]),
                    "start_m": int(st["start_m"]),
                    "end_h": int(st["end_h"]),
                    "end_m": int(st["end_m"]),
                    "created_by": str(interaction.user.id),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

                try:
                    await db_to_thread(lambda: db_upsert_panel(panel_row))
                    pres = await db_to_thread(lambda: db_get_panel(guild_id, day_key))
                except Exception as e:
                    await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
                    return

                if not pres.data:
                    await interaction.followup.send("❌ panels の取得に失敗した（DBを確認してね）", ephemeral=True)
                    return

                panel = pres.data[0]
                panel_id = int(panel["id"])

                # slots 作り直し（重複対策：先に削除）
                try:
                    await db_to_thread(lambda: db_delete_slots(panel_id))
                except Exception:
                    pass

                # slots 作成
                slot_rows = []
                cur = start_dt
                while cur < end_dt:
                    st_utc = cur.astimezone(timezone.utc)
                    en_utc = (cur + timedelta(minutes=interval)).astimezone(timezone.utc)
                    slot_rows.append(
                        {
                            "panel_id": panel_id,
                            "start_at": st_utc.isoformat(),
                            "end_at": en_utc.isoformat(),
                            "slot_time": cur.strftime("%H:%M"),  # ✅ slots.slot_time NOT NULL 対策
                            "is_break": False,
                            "notified": False,
                            "reserved_by": None,
                        }
                    )
                    cur += timedelta(minutes=interval)

                try:
                    await db_to_thread(lambda: db_insert_slots(slot_rows))
                except Exception as e:
                    await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)
                    return

                # 投稿先ch
                ch = interaction.guild.get_channel(int(notify_channel_id)) or interaction.channel

                # slots 読み直し
                sres = await db_to_thread(lambda: db_get_slots(panel_id))
                slots = sres.data or []

                # @everyone は「作成時1回だけ」
                content = "@everyone 募集を開始しました！" if mention_everyone else None

                msg = await ch.send(
                    content=content,
                    embed=build_panel_embed(panel, slots),
                    view=SlotsView(panel_id, slots),
                )

                # message_id保存（失敗してもOK）
                try:
                    await db_to_thread(lambda: db_update_panel_message_id(panel_id, str(msg.id)))
                except Exception:
                    pass

                await interaction.followup.send("✅ 募集パネルを投稿した！", ephemeral=True)
                return

            # setup:* でここに来たらとりあえずACK（事故防止）
            if not interaction.response.is_done():
                await interaction.response.defer()
            return

        # ---- slot buttons ----
        if cid.startswith("slot:"):
            # まずACK
            await interaction.response.defer(ephemeral=True)

            slot_id = int(cid.split(":")[1])
            user_id = str(interaction.user.id)

            # slot取得
            sres = await db_to_thread(lambda: db_get_slot(slot_id))
            if not sres.data:
                await interaction.followup.send("❌ 枠が見つからない（古いボタンかも）", ephemeral=True)
                return
            slot = sres.data[0]

            if bool(slot.get("is_break", False)):
                await interaction.followup.send("❌ 休憩枠は予約できないよ", ephemeral=True)
                return

            reserved_by = slot.get("reserved_by")

            # 空き→予約
            if not reserved_by:
                ures = await db_to_thread(lambda: db_try_reserve(slot_id, user_id))
                if not ures.data:
                    await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
                    return
                await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

            # 予約済み→本人ならキャンセル
            else:
                if str(reserved_by) != user_id:
                    await interaction.followup.send("❌ 他人の予約はキャンセルできないよ", ephemeral=True)
                    return
                cres = await db_to_thread(lambda: db_try_cancel(slot_id, user_id))
                if not cres.data:
                    await interaction.followup.send("❌ キャンセルに失敗した（もう変わってるかも）", ephemeral=True)
                    return
                await interaction.followup.send("✅ キャンセルしたよ！", ephemeral=True)

            # パネルを更新（同じメッセージを編集）
            # panel_id は slots.panel_id
            panel_id = int(slot["panel_id"])

            # panel取得
            # day_key は panel側にあるので guild_id から引っ張るより、panel_idで取るのが理想だが
            # テーブル構造が不明なので、ここは slots→panel_id から panels を直接引く
            def get_panel_by_id(pid: int):
                return sb.table("panels").select("*").eq("id", pid).limit(1).execute()

            pres = await db_to_thread(lambda: get_panel_by_id(panel_id))
            if not pres.data:
                return
            panel = pres.data[0]

            # slots再取得
            sres2 = await db_to_thread(lambda: db_get_slots(panel_id))
            slots2 = sres2.data or []

            # 押されたメッセージを更新
            try:
                await interaction.message.edit(  # type: ignore
                    embed=build_panel_embed(panel, slots2),
                    view=SlotsView(panel_id, slots2),
                )
            except Exception:
                pass

            return

        # ---- other components ----
        if not interaction.response.is_done():
            await interaction.response.defer()

    except Exception:
        # ここで落ちると「アプリが応答しませんでした」になるので絶対握る
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 内部エラー（もう一回押して）", ephemeral=True)
        except Exception:
            pass


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


async def main():
    # 429避け（必要なら）
    await asyncio.sleep(2)
    await client.start(TOKEN)


asyncio.run(main())