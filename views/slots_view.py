from __future__ import annotations

from datetime import datetime, timedelta, timezone
import discord

JST = timezone(timedelta(hours=9))

def slot_time_label(dt_utc: datetime) -> str:
    return dt_utc.astimezone(JST).strftime("%H:%M")

class SlotsView(discord.ui.View):
    def __init__(self, sb, db_to_thread, panel_id: int, slot_rows: list[dict]):
        super().__init__(timeout=None)
        self.sb = sb
        self.db_to_thread = db_to_thread

        # ボタンは最大25。まずは20に制限（安定）
        for r in slot_rows[:20]:
            sid = int(r["id"])
            st = datetime.fromisoformat(str(r["start_at"]).replace("Z", "+00:00"))
            label = slot_time_label(st)
            self.add_item(SlotButton(label=label, slot_id=sid, sb=sb, db_to_thread=db_to_thread))

class SlotButton(discord.ui.Button):
    def __init__(self, label: str, slot_id: int, sb, db_to_thread):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"slot:{slot_id}")
        self.slot_id = slot_id
        self.sb = sb
        self.db_to_thread = db_to_thread

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        # 予約済みチェック
        def work_get():
            return self.sb.table("slots").select("reserved_by").eq("id", self.slot_id).limit(1).execute()

        res = await self.db_to_thread(work_get)
        if res.data and res.data[0].get("reserved_by"):
            await interaction.followup.send("❌ その枠はすでに予約されています", ephemeral=True)
            return

        # 予約
        def work_set():
            return self.sb.table("slots").update({"reserved_by": user_id}).eq("id", self.slot_id).execute()

        await self.db_to_thread(work_set)
        await interaction.followup.send("✅ 予約したよ！", ephemeral=True)