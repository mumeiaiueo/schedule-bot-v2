import os
import asyncio
from supabase import create_client

sb = None

def init_supabase():
    global sb
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")

    print("SUPABASE_URL set? =", bool(url))
    print("SUPABASE_KEY set? =", bool(key))

    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY が未設定です")

    sb = create_client(url, key)
    print("✅ Supabase client created")
    return sb

async def db_to_thread(fn):
    return await asyncio.to_thread(fn)