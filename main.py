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

# ========= utils =========
draft = {}  # key: (guild_id, user_id) -> dict

def dkey(i: discord.Interaction):
    return (str(i.guild_id), str(i.user.id))

async def db_to_thread(fn):
    return await asyncio.to_thread(fn)

def jst_now():
    return datetime.now(JST)

def day_key_from_choice(choice: str) -> str:
    # choice: "today" or "tomorrow"
    base = date.today()
    if choice == "tomorrow":
        base = base + timedelta(days=1)
    return base.isoformat()  # "YYYY-MM-DD"

def hm_label(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%H:%M")

# ========= DB helpers =========
def upsert_panel(row: dict):
    # panels に (guild_id, day_key) のユニーク制約がある想定
    return sb.table("panels").upsert(row, on_conflict="guild_id,day_key").execute()

def get_panel(guild_id: str, day_key: str):
    return sb.table("panels").select("*").eq("guild_id", guild_id).eq("day_key", day_key).limit(1).execute()

def update_panel_message_id(panel_id: int, message_id: str):
    return sb.table("panels").update({"panel_message_id": message_id}).eq("id", panel_id).execute()

def delete_panel_and_slots(guild_id: str, day_key: str):
    pres = sb.table("panels").select("id").eq("guild_id", guild_id).eq("day_key", day_key).limit(1).execute()
    if pres.data:
        panel_id = pres.data[0]["id"]
        sb.table("slots").delete().eq("panel_id", panel_id).execute()
        sb.table("panels").delete().eq("id", panel_id).execute()
    return True

def delete_slots(panel_id: int):
    return sb.table("slots").delete().eq("panel_id", panel_id).execute()

def insert_slots(rows: list[dict]):
    # 既存ユニーク(panel_id,start_at)に当たったら嫌なので、
    # 事前 delete_slots(panel_id) を必ず呼ぶ運用にする
    return sb.table("slots").insert(rows).execute()

def set_manager_role(guild_id: str, role_id: int | None):
    row = {"guild_id": guild_id, "manager_role_id": role_id}
    return sb.table("guild_settings").upsert(row, on_conflict="guild_id").execute()

def get_manager_role_id(guild_id: str) -> int | None:
    res = sb.table("guild_settings").select("manager_role_id").eq("guild_id", guild_id).limit(1).execute()
    if res.data and res.data[0].get("manager_role_id"):
        return int(res.data[0]["manager_role_id"])
    return None

def is_manager(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    rid = get_manager_role_id(str(interaction.guild_id))
    if not rid:
        return False
    return any(r.id == rid for r in getattr(interaction.user, "roles", []))

# ========= embeds =========
def build_setup_embed_step1(st: dict) -> discord.Embed:
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)
    e.description = (
        "Step 1\n"
        f"日付\n{ '今日' if st['day_choice']=='today' else '明日' }\n"
        f"開始\n{st['start_h']:02d}:{st['start_m']:02d}\n"
        f"終了\n{st['end_h']:02d}:{st['end_m']:02d}\n\n"
        "ボタン/セレクトで設定して「次へ」"
    )
    return e

def build_setup_embed_step2(st: dict) -> discord.Embed:
    e = discord.Embed(title="募集パネル作成ウィザード", color=0x5865F2)
    e.description = (
        "Step 2\n"
        f"日付\n{ '今日' if st['day_choice']=='today' else '明日' }\n"
        f"開始\n{st['start_h']:02d}:{st['start_m']:02d}\n"
        f"終了\n{st['end_h']:02d}:{st['end_m']:02d}\n"
        f"間隔\n{st['interval']}分\n"
        f"タイトル\n{st['title']}\n"
        f"@everyone\n{'ON' if st['mention_everyone'] else 'OFF'}\n"
        f"通知チャンネル\n{st['notify_channel_name']}\n\n"
        "ボタン/セレクトで設定して「作成」"
    )
    return e

def build_panel_embed(panel: dict, slots: list[dict]) -> discord.Embed:
    # 画像のイメージに近い感じでリスト表示（最大8行くらい）
    e = discord.Embed(title="募集パネル", color=0x2b2d31)
    e.description = f"📅 {panel['day_key']}（JST） / interval {panel['interval_minutes']}min"

    lines = []
    # start_at順に並べて表示
    def to_dt(r):
        return datetime.fromisoformat(str(r["start_at"]).replace("Z", "+00:00"))
    for r in sorted(slots, key=to_dt)[:10]:
        t = hm_label(to_dt(r))
        # reserved_by が入ってたら赤っぽく見せたいけど、Embedは絵文字で表現
        if r.get("is_break"):
            lines.append(f"⚪ {t} 休憩")
        elif r.get("reserved_by") or r.get("reserver_user_id"):
            # どっちの列が使われててもOKにする
            uid = r.get("reserved_by") or r.get("reserver_user_id")
            lines.append(f"🔴 {t} <@{uid}>")
        else:
            lines.append(f"🟢 {t}")
    e.add_field(
        name="枠一覧",
        value="\n".join(lines) if lines else "（まだ枠がありません）",
        inline=False
    )
    e.set_footer(text="🟢空き / 🔴予約済み（本人は押すとキャンセル） / ⚪休憩（予約不可）")
    return e

# ========= UI parts =========
class HourSelect(discord.ui.Select):
    def __init__(self, custom_id: str, placeholder: str, default: int):
        options = [discord.SelectOption(label=f"{h:02d}", value=str(h), default=(h==default)) for h in range(0,24)]
        super().__init__(custom_id=custom_id, placeholder=placeholder, min_values=1, max_values=1, options=options)

class MinuteSelect(discord.ui.Select):
    def __init__(self, custom_id: str, placeholder: str, default: int):
        mins = [0,5,10,15,20,25,30,35,40,45,50,55]
        options = [discord.SelectOption(label=f"{m:02d}", value=str(m), default=(m==default)) for m in mins]
        super().__init__(custom_id=custom_id, placeholder=placeholder, min_values=1, max_values=1, options=options)

class IntervalSelect(discord.ui.Select):
    def __init__(self, default: int):
        vals = [20,25,30]
        options = [discord.SelectOption(label=f"{v}分", value=str(v), default=(v==default)) for v in vals]
        super().__init__(custom_id="setup:interval_select", placeholder="間隔（20/25/30）", min_values=1, max_values=1, options=options)

class NotifyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（未選択=このチャンネル）",
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )

class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(label="タイトル", placeholder="例：夕方", max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        st = draft.setdefault(dkey(interaction), {})
        st["title"] = str(self.name.value) if self.name.value else "無題"
        await interaction.response.send_message("✅ タイトルを保存しました", ephemeral=True)

# ========= Step1 View =========
class SetupStep1View(discord.ui.View):
    def __init__(self, owner_key):
        super().__init__(timeout=None)
        self.owner_key = owner_key
        st = draft[self.owner_key]

        self.add_item(HourSelect("setup:start_h", "開始(時) 例:19", st["start_h"]))
        self.add_item(MinuteSelect("setup:start_m", "開始(分) 例:00", st["start_m"]))
        self.add_item(HourSelect("setup:end_h", "終了(時) 例:21", st["end_h"]))
        self.add_item(MinuteSelect("setup:end_m", "終了(分) 例:00", st["end_m"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return dkey(interaction) == self.owner_key

    @discord.ui.button(label="今日", style=discord.ButtonStyle.primary, custom_id="setup:today")
    async def today_btn(self, interaction: discord.Interaction, _):
        draft[self.owner_key]["day_choice"] = "today"
        await interaction.response.edit_message(embed=build_setup_embed_step1(draft[self.owner_key]), view=self)

    @discord.ui.button(label="明日", style=discord.ButtonStyle.secondary, custom_id="setup:tomorrow")
    async def tomorrow_btn(self, interaction: discord.Interaction, _):
        draft[self.owner_key]["day_choice"] = "tomorrow"
        await interaction.response.edit_message(embed=build_setup_embed_step1(draft[self.owner_key]), view=self)

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next")
    async def next_btn(self, interaction: discord.Interaction, _):
        # Step2へ
        await interaction.response.edit_message(
            embed=build_setup_embed_step2(draft[self.owner_key]),
            view=SetupStep2View(self.owner_key)
        )

    @discord.ui.select(custom_id="setup:start_h")
    async def _dummy1(self, interaction: discord.Interaction, select: discord.ui.Select): ...
    @discord.ui.select(custom_id="setup:start_m")
    async def _dummy2(self, interaction: discord.Interaction, select: discord.ui.Select): ...
    @discord.ui.select(custom_id="setup:end_h")
    async def _dummy3(self, interaction: discord.Interaction, select: discord.ui.Select): ...
    @discord.ui.select(custom_id="setup:end_m")
    async def _dummy4(self, interaction: discord.Interaction, select: discord.ui.Select): ...

    async def on_timeout(self):  # noqa
        return

# select callbackをまとめて拾うためのグローバルハンドラ
@client.event
async def on_interaction(interaction: discord.Interaction):
    # 通常処理はdiscord.pyがやるので、ここでは Select値の反映だけ
    try:
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id")  # type: ignore
        if not cid or not cid.startswith("setup:"):
            return
        key = dkey(interaction)
        if key not in draft:
            return

        st = draft[key]
        # Select系
        if cid in ("setup:start_h","setup:start_m","setup:end_h","setup:end_m","setup:interval_select"):
            val = interaction.data["values"][0]  # type: ignore
            if cid == "setup:start_h":
                st["start_h"] = int(val)
            elif cid == "setup:start_m":
                st["start_m"] = int(val)
            elif cid == "setup:end_h":
                st["end_h"] = int(val)
            elif cid == "setup:end_m":
                st["end_m"] = int(val)
            elif cid == "setup:interval_select":
                st["interval"] = int(val)

            # いま表示中のStepに応じてembed更新
            # （Step番号は st["step"] で管理）
            if st.get("step") == 1:
                await interaction.response.edit_message(embed=build_setup_embed_step1(st), view=interaction.message.components)  # type: ignore
            else:
                await interaction.response.edit_message(embed=build_setup_embed_step2(st), view=interaction.message.components)  # type: ignore
    except Exception:
        # ここは失敗しても致命じゃないので握る（UI更新できないだけ）
        return

# ========= Step2 View =========
class SetupStep2View(discord.ui.View):
    def __init__(self, owner_key):
        super().__init__(timeout=None)
        self.owner_key = owner_key
        st = draft[self.owner_key]
        self.add_item(IntervalSelect(st["interval"]))
        self.add_item(NotifyChannelSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return dkey(interaction) == self.owner_key

    @discord.ui.button(label="📝 タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title")
    async def title_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(TitleModal())

    @discord.ui.button(label="@everyone ON", style=discord.ButtonStyle.danger, custom_id="setup:everyone")
    async def everyone_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = draft[self.owner_key]
        st["mention_everyone"] = not st["mention_everyone"]
        button.label = "@everyone ON" if st["mention_everyone"] else "@everyone OFF"
        await interaction.response.edit_message(embed=build_setup_embed_step2(st), view=self)

    @discord.ui.button(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back")
    async def back_btn(self, interaction: discord.Interaction, _):
        st = draft[self.owner_key]
        st["step"] = 1
        await interaction.response.edit_message(
            embed=build_setup_embed_step1(st),
            view=SetupStep1View(self.owner_key)
        )

    @discord.ui.button(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create")
    async def create_btn(self, interaction: discord.Interaction, _):
        st = draft[self.owner_key]

        # 通知チャンネル選択の反映（ChannelSelectは values に channel_id）
        # ここだけ interaction.data を直接見るのは難しいので、
        # “未選択ならこのチャンネル”で運用（選択したい場合は次のメッセで対応する）
        notify_channel_id = st.get("notify_channel_id") or str(interaction.channel_id)

        day_key = day_key_from_choice(st["day_choice"])
        title = st["title"]
        interval = int(st["interval"])
        mention_everyone = bool(st["mention_everyone"])

        # JSTで開始/終了を作る（またぎ対応：終了<=開始なら翌日扱い）
        base = datetime.fromisoformat(day_key + "T00:00:00").replace(tzinfo=JST)
        start_dt = base.replace(hour=st["start_h"], minute=st["start_m"])
        end_dt = base.replace(hour=st["end_h"], minute=st["end_m"])
        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)

        row = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),
            "day_key": day_key,
            "title": title,
            "interval_minutes": interval,
            "notify_channel_id": notify_channel_id,
            "mention_everyone": mention_everyone,
            "created_by": str(interaction.user.id),
            "created_at": datetime.utcnow().isoformat(),
        }

        await interaction.response.defer(ephemeral=True)

        # 1) panels upsert
        try:
            pres = await db_to_thread(lambda: upsert_panel(row))
        except Exception as e:
            await interaction.followup.send(f"❌ panels 保存失敗: {e}", ephemeral=True)
            return

        # upsert結果から panel_id を取りたいので再取得
        try:
            got = await db_to_thread(lambda: get_panel(str(interaction.guild_id), day_key))
        except Exception as e:
            await interaction.followup.send(f"❌ panels 再取得失敗: {e}", ephemeral=True)
            return
        if not got.data:
            await interaction.followup.send("❌ panels が見つからない", ephemeral=True)
            return

        panel = got.data[0]
        panel_id = int(panel["id"])

        # 2) 既存slots削除 → 作り直し（unique対策）
        try:
            await db_to_thread(lambda: delete_slots(panel_id))
        except Exception:
            pass

        # 3) slots 生成（slot_time NOT NULL 対応）
        slot_rows = []
        cur = start_dt
        while cur < end_dt:
            start_utc = cur.astimezone(timezone.utc)
            end_utc = (cur + timedelta(minutes=interval)).astimezone(timezone.utc)

            slot_rows.append({
                "panel_id": panel_id,
                "start_at": start_utc.isoformat(),
                "end_at": end_utc.isoformat(),
                "slot_time": hm_label(cur),   # ← 必須
                "is_break": False,            # ← NOT NULL
                "notified": False,            # ← NOT NULL
                # 予約列はテーブルにより名前が違うので両対応にしない（NULLならOK）
                "reserved_by": None,
                "reserver_user_id": None,
                "reserver_name": None,
                "reserved_at": None,
            })
            cur += timedelta(minutes=interval)

        try:
            ins = await db_to_thread(lambda: insert_slots(slot_rows))
        except Exception as e:
            await interaction.followup.send(f"❌ slots 作成失敗: {e}", ephemeral=True)
            return

        created = ins.data or []
        if not created:
            await interaction.followup.send("❌ slots が作れなかった（列名が違う可能性）", ephemeral=True)
            return

        # 4) パネル投稿
        ch = interaction.guild.get_channel(int(notify_channel_id)) or interaction.channel

        content = ""
        if mention_everyone:
            content = "@everyone 募集を開始しました！"

        msg = await ch.send(
            content=content,
            embed=build_panel_embed(panel, created),
            view=PanelView(panel_id=panel_id),
        )

        try:
            await db_to_thread(lambda: update_panel_message_id(panel_id, str(msg.id)))
        except Exception:
            pass

        await interaction.followup.send("✅ 作成して募集パネルを投稿した！", ephemeral=True)

# ========= Panel View（枠ボタン + 通知ON + 休憩切替） =========
class PanelView(discord.ui.View):
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)
        self.panel_id = panel_id

    # 枠ボタンはDBから動的に作りたいけど、View生成時にDB読めないので
    # いったん「最新20枠だけをリフレッシュ」ボタン方式にするのが安全。
    # ただ、あなたは “ボタン並べたい” ので、今回は簡易版で「枠ボタン20個」を作る。
    # → 実運用で増やすなら “ページ切替” にする。

    async def rebuild(self, interaction: discord.Interaction):
        # slotsを取り直してボタンを作り直す
        def work():
            return sb.table("slots").select("*").eq("panel_id", self.panel_id).order("start_at").limit(20).execute()
        res = await db_to_thread(work)
        rows = res.data or []

        self.clear_items()

        # 枠ボタン
        for r in rows:
            sid = int(r["id"])
            label = r.get("slot_time") or "??:??"
            is_break = bool(r.get("is_break", False))
            reserved = bool(r.get("reserved_by") or r.get("reserver_user_id"))

            style = discord.ButtonStyle.success
            if is_break:
                style = discord.ButtonStyle.secondary
            elif reserved:
                style = discord.ButtonStyle.danger

            self.add_item(SlotButton(label=label, slot_id=sid, style=style))

        # 通知ON/OFF（ダミー：panel側にフラグ持たせたいけど今はボタンだけ）
        self.add_item(NotifyToggleButton())

        # 休憩切替（管理者/管理ロール）
        self.add_item(BreakToggleButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 誰でも押せる（管理系は各ボタンで弾く）
        return True

    async def on_timeout(self):  # noqa
        return

class SlotButton(discord.ui.Button):
    def __init__(self, label: str, slot_id: int, style: discord.ButtonStyle):
        super().__init__(label=label, style=style, custom_id=f"slot:{slot_id}")
        self.slot_id = slot_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)

        # 最新取得
        def get_one():
            return sb.table("slots").select("*").eq("id", self.slot_id).limit(1).execute()
        res = await db_to_thread(get_one)
        if not res.data:
            await interaction.followup.send("❌ 枠が見つからない", ephemeral=True)
            return
        r = res.data[0]

        if bool(r.get("is_break", False)):
            await interaction.followup.send("❌ 休憩枠は予約できない", ephemeral=True)
            return

        reserved_by = r.get("reserved_by") or r.get("reserver_user_id")
        if reserved_by:
            # 本人ならキャンセル
            if str(reserved_by) == uid:
                def cancel():
                    return sb.table("slots").update({
                        "reserved_by": None,
                        "reserver_user_id": None,
                        "reserver_name": None,
                        "reserved_at": None,
                    }).eq("id", self.slot_id).execute()
                await db_to_thread(cancel)
                await interaction.followup.send("✅ キャンセルしたよ！", ephemeral=True)
            else:
                await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
            return

        # 予約
        def reserve():
            return sb.table("slots").update({
                "reserved_by": uid,                 # どっちかの列があれば入る
                "reserver_user_id": int(uid),       # bigintの列があるなら入る
                "reserved_at": datetime.utcnow().isoformat(),
            }).eq("id", self.slot_id).execute()
        await db_to_thread(reserve)
        await interaction.followup.send("✅ 予約したよ！", ephemeral=True)

class NotifyToggleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔔 通知ON", style=discord.ButtonStyle.success, custom_id="panel:notify")

    async def callback(self, interaction: discord.Interaction):
        # 本当は panels に notify_enabled を持たせたい。
        # 今回は見た目だけ（次で実装する）
        await interaction.response.send_message("✅（仮）通知ON/OFFは次で実装するよ", ephemeral=True)

class BreakToggleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🛠 休憩切替（管理者/管理ロール）", style=discord.ButtonStyle.secondary, custom_id="panel:break")

    async def callback(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            await interaction.response.send_message("❌ 管理者/管理ロールのみ操作できます", ephemeral=True)
            return
        await interaction.response.send_message("✅（仮）休憩切替UIは次で実装するよ", ephemeral=True)

# ========= Commands =========
@tree.command(name="setup", description="募集パネル作成ウィザードを開く")
async def setup(interaction: discord.Interaction):
    # 初期値（画像のStep1っぽく）
    now = jst_now()
    key = dkey(interaction)
    draft[key] = {
        "step": 1,
        "day_choice": "today",
        "start_h": now.hour,
        "start_m": (now.minute // 5) * 5,
        "end_h": (now + timedelta(hours=3)).hour,
        "end_m": (now.minute // 5) * 5,
        "interval": 25,
        "title": "無題",
        "mention_everyone": False,
        "notify_channel_id": str(interaction.channel_id),
        "notify_channel_name": f"#{interaction.channel.name}",
    }

    await interaction.response.send_message(
        embed=build_setup_embed_step1(draft[key]),
        view=SetupStep1View(key),
        ephemeral=True
    )

@tree.command(name="reset", description="今日or明日の募集を削除（管理者/管理ロール）")
async def reset(interaction: discord.Interaction):
    if not is_manager(interaction):
        await interaction.response.send_message("❌ 管理者/管理ロールのみ実行できます", ephemeral=True)
        return

    view = ResetView()
    await interaction.response.send_message("どっちを削除する？", view=view, ephemeral=True)

class ResetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="今日", style=discord.ButtonStyle.danger)
    async def today(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        day_key = date.today().isoformat()
        await db_to_thread(lambda: delete_panel_and_slots(str(interaction.guild_id), day_key))
        await interaction.followup.send("✅ 今日の募集を削除した", ephemeral=True)

    @discord.ui.button(label="明日", style=discord.ButtonStyle.danger)
    async def tomorrow(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        day_key = (date.today() + timedelta(days=1)).isoformat()
        await db_to_thread(lambda: delete_panel_and_slots(str(interaction.guild_id), day_key))
        await interaction.followup.send("✅ 明日の募集を削除した", ephemeral=True)

@tree.command(name="manager_role", description="管理ロールを設定/解除（管理者のみ）")
@app_commands.describe(role="管理ロール（解除するなら未指定）")
async def manager_role(interaction: discord.Interaction, role: discord.Role | None = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ サーバー管理者のみ設定できます", ephemeral=True)
        return
    rid = int(role.id) if role else None
    await interaction.response.defer(ephemeral=True)
    await db_to_thread(lambda: set_manager_role(str(interaction.guild_id), rid))
    await interaction.followup.send(f"✅ 管理ロールを {'解除' if rid is None else f'<@&{rid}>'} に設定した", ephemeral=True)

# ========= lifecycle =========
@client.event
async def on_ready():
    # 永続View（ボタンが古いメッセでも動く）
    client.add_view(PanelView(panel_id=0))  # ダミー登録（discord.py都合）
    await tree.sync()
    print(f"✅ Logged in as {client.user}")

async def main():
    await asyncio.sleep(5)  # 429避け
    await client.start(TOKEN)

asyncio.run(main())