import os
import asyncio
from datetime import datetime, timedelta, timezone

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
draft = {}  # key: (guild_id, user_id) -> dict


def dkey(interaction: discord.Interaction):
    return (str(interaction.guild_id), str(interaction.user.id))


async def db_to_thread(fn):
    return await asyncio.to_thread(fn)


# ========= DB helpers =========
def upsert_panel(row: dict):
    # panels に (guild_id, day_key) のユニーク制約がある前提
    return sb.table("panels").upsert(row, on_conflict="guild_id,day_key").execute()


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


def hm_from_state(st: dict, prefix: str):
    h = st.get(f"{prefix}_h")
    m = st.get(f"{prefix}_m")
    if h is None or m is None:
        return None
    return f"{int(h):02d}:{int(m):02d}"


def build_setup_embed(st: dict):
    # Step表示は st["step"] で切替
    step = int(st.get("step", 1))

    e = discord.Embed(
        title="募集パネル作成ウィザード",
        description="ボタン/セレクトで設定して「作成」",
        color=0x5865F2,
    )
    e.add_field(name="Step", value=str(step), inline=False)

    day = st.get("day_key", "today")
    e.add_field(name="日付", value=("今日" if day == "today" else "明日"), inline=True)

    start = hm_from_state(st, "start")
    end = hm_from_state(st, "end")
    e.add_field(name="開始", value=(start or "未選択"), inline=True)
    e.add_field(name="終了", value=(end or "未選択"), inline=True)

    interval = st.get("interval_minutes")
    e.add_field(name="間隔", value=(f"{interval}分" if interval else "未選択"), inline=True)

    title = st.get("title") or "無題"
    e.add_field(name="タイトル", value=title, inline=False)

    notify = st.get("notify_channel_id")
    e.add_field(name="通知チャンネル", value=(f"<#{notify}>" if notify else "このチャンネル"), inline=False)

    everyone = bool(st.get("mention_everyone", False))
    e.add_field(name="@everyone", value=("ON" if everyone else "OFF"), inline=True)

    e.set_footer(text="Step1→「次へ」 / Step2→「作成」")
    return e


def validate_setup(st: dict):
    start = hm_from_state(st, "start")
    end = hm_from_state(st, "end")
    interval = st.get("interval_minutes")
    if not start or not end or not interval:
        return False
    return True


# ========= Modal =========
class TitleModal(discord.ui.Modal, title="タイトル入力"):
    name = discord.ui.TextInput(
        label="タイトル",
        placeholder="例：夕方",
        max_length=50,
        required=False,
    )

    def __init__(self, st: dict):
        super().__init__(timeout=300)
        self.st = st

    async def on_submit(self, interaction: discord.Interaction):
        self.st["title"] = (self.name.value or "").strip() or "無題"
        # モーダルはメッセージ編集できないので ephemeral でOK
        await interaction.response.send_message("✅ タイトルを保存しました", ephemeral=True)


# ========= Components =========
class DayButton(discord.ui.Button):
    def __init__(self, st: dict, label: str, day_key: str, style: discord.ButtonStyle, row: int):
        super().__init__(label=label, style=style, custom_id=f"setup:day:{day_key}", row=row)
        self.st = st
        self.day_key = day_key

    async def callback(self, interaction: discord.Interaction):
        self.st["day_key"] = self.day_key
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=self.view)


class NextButton(discord.ui.Button):
    def __init__(self, st: dict, row: int):
        super().__init__(label="次へ", style=discord.ButtonStyle.success, custom_id="setup:next", row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        # Step1 -> Step2
        self.st["step"] = 2
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupView(self.st))


class BackButton(discord.ui.Button):
    def __init__(self, st: dict, row: int):
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, custom_id="setup:back", row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        self.st["step"] = 1
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=SetupView(self.st))


class StartHSelect(discord.ui.Select):
    def __init__(self, st: dict, row: int):
        super().__init__(custom_id="setup:start_h", placeholder="開始(時)", options=hour_options(), row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        self.st["start_h"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=self.view)


class StartMSelect(discord.ui.Select):
    def __init__(self, st: dict, row: int):
        super().__init__(custom_id="setup:start_m", placeholder="開始(分)", options=minute_options(5), row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        self.st["start_m"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=self.view)


class EndHSelect(discord.ui.Select):
    def __init__(self, st: dict, row: int):
        super().__init__(custom_id="setup:end_h", placeholder="終了(時)", options=hour_options(), row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        self.st["end_h"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=self.view)


class EndMSelect(discord.ui.Select):
    def __init__(self, st: dict, row: int):
        super().__init__(custom_id="setup:end_m", placeholder="終了(分)", options=minute_options(5), row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        self.st["end_m"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=self.view)


class IntervalSelect(discord.ui.Select):
    def __init__(self, st: dict, row: int):
        super().__init__(custom_id="setup:interval", placeholder="間隔（20/25/30）", options=interval_options(), row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        self.st["interval_minutes"] = int(self.values[0])
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=self.view)


class TitleButton(discord.ui.Button):
    def __init__(self, st: dict, row: int):
        super().__init__(label="タイトル入力", style=discord.ButtonStyle.secondary, custom_id="setup:title", row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TitleModal(self.st))


class EveryoneToggleButton(discord.ui.Button):
    def __init__(self, st: dict, row: int):
        super().__init__(label="@everyone ON/OFF", style=discord.ButtonStyle.danger, custom_id="setup:everyone", row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        self.st["mention_everyone"] = not bool(self.st.get("mention_everyone", False))
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=self.view)


class NotifyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, st: dict, row: int):
        super().__init__(
            custom_id="setup:notify_channel",
            placeholder="通知チャンネル（未選択=このチャンネル）",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            row=row,
        )
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        ch = self.values[0]
        self.st["notify_channel_id"] = str(ch.id)
        await interaction.response.edit_message(embed=build_setup_embed(self.st), view=self.view)


class CreateButton(discord.ui.Button):
    def __init__(self, st: dict, row: int):
        super().__init__(label="作成", style=discord.ButtonStyle.success, custom_id="setup:create", row=row)
        self.st = st

    async def callback(self, interaction: discord.Interaction):
        # まずACK
        await interaction.response.defer(ephemeral=True)

        # 必須チェック
        if not validate_setup(self.st):
            await interaction.followup.send("❌ 開始/終了/間隔が未選択。/setup からやり直してね", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        channel_id = str(interaction.channel_id)
        day_key = self.st.get("day_key", "today")

        start_hm = hm_from_state(self.st, "start")
        end_hm = hm_from_state(self.st, "end")
        interval = int(self.st.get("interval_minutes"))
        title = self.st.get("title") or "無題"
        mention_everyone = bool(self.st.get("mention_everyone", False))
        notify_channel_id = self.st.get("notify_channel_id")  # None可

        row = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "day_key": day_key,
            "title": title,
            "interval_minutes": interval,
            "notify_channel_id": notify_channel_id,
            "mention_everyone": mention_everyone,

            # ★ ここが今回の肝：開始/終了を panels に保存
            "start_hm": start_hm,
            "end_hm": end_hm,

            "created_by": str(interaction.user.id),
            "created_at": datetime.utcnow().isoformat(),
        }

        try:
            await db_to_thread(lambda: upsert_panel(row))
        except Exception as e:
            # panels に列が無いパターンが多いので、分かりやすく返す
            msg = str(e)
            if "Could not find the 'start_hm' column" in msg or "Could not find the 'end_hm' column" in msg:
                await interaction.followup.send(
                    "❌ 保存失敗：panels に start_hm / end_hm 列がありません。\n"
                    "Supabase の SQL で列追加してからもう一度 /setup してね（下にSQL貼ってある）",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(f"❌ 保存失敗: {e}", ephemeral=True)
            return

        await interaction.followup.send("✅ 保存できた！次は /generate（枠ボタン生成）に進めるよ", ephemeral=True)


# ========= View =========
class SetupView(discord.ui.View):
    def __init__(self, st: dict):
        super().__init__(timeout=None)
        self.st = st

        step = int(st.get("step", 1))

        if step == 1:
            # Step1: 日付/開始/終了/次へ
            self.add_item(DayButton(st, "今日", "today", discord.ButtonStyle.primary, row=0))
            self.add_item(DayButton(st, "明日", "tomorrow", discord.ButtonStyle.secondary, row=0))
            self.add_item(NextButton(st, row=0))

            self.add_item(StartHSelect(st, row=1))
            self.add_item(StartMSelect(st, row=2))
            self.add_item(EndHSelect(st, row=3))
            self.add_item(EndMSelect(st, row=4))

        else:
            # Step2: 間隔/タイトル/@everyone/通知ch/作成
            self.add_item(IntervalSelect(st, row=0))
            self.add_item(TitleButton(st, row=1))
            self.add_item(EveryoneToggleButton(st, row=1))
            self.add_item(NotifyChannelSelect(st, row=2))

            self.add_item(BackButton(st, row=3))
            self.add_item(CreateButton(st, row=3))


# ========= command =========
@tree.command(name="setup", description="募集パネルを作る（ウィザード）")
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
    }
    st = draft[key]
    await interaction.response.send_message(
        "設定して「作成」👇",
        embed=build_setup_embed(st),
        view=SetupView(st),
        ephemeral=False,
    )


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")


async def main():
    await client.start(TOKEN)


asyncio.run(main())