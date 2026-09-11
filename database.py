# -*- coding: utf-8 -*-
"""
MongoDB (Atlas) bilan ishlash uchun barcha funksiyalar shu yerda.
Ulanish manzili MONGO_URI muhit o'zgaruvchisidan olinadi (Render -> Environment).
"""
import os
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb+srv://d01651800_db_user:mukxa@cluster0.1u4imcy.mongodb.net/?appName=Cluster0",
)
DB_NAME = os.getenv("DB_NAME", "konkurs_bot")

_client = AsyncIOMotorClient(MONGO_URI)
_db = _client[DB_NAME]
users_col = _db["users"]
channels_col = _db["channels"]


async def init_db():
    """Kerakli indekslarni yaratadi (bir marta chaqiriladi)."""
    await users_col.create_index("user_id", unique=True)
    await channels_col.create_index("chat_id", unique=True)


# ---------------- USERS ----------------

async def user_exists(user_id: int) -> bool:
    return await users_col.find_one({"user_id": user_id}) is not None


async def add_user(user_id: int, username: str, full_name: str, referrer_id):
    if await user_exists(user_id):
        return
    await users_col.insert_one({
        "user_id": user_id,
        "username": username,
        "full_name": full_name,
        "referrer_id": referrer_id,
        "referral_count": 0,
        "is_confirmed": False,
        "joined_at": datetime.now(timezone.utc).isoformat(),
    })


async def get_user(user_id: int):
    return await users_col.find_one({"user_id": user_id})


async def confirm_user(user_id: int):
    """
    Foydalanuvchi majburiy obunani birinchi marta bajarganda chaqiriladi.
    Agar bu birinchi tasdiqlash bo'lsa va u kimningdir referali bo'lsa,
    referrer_id ni qaytaradi, aks holda None.
    """
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        return None
    if user.get("is_confirmed"):
        return None

    await users_col.update_one({"user_id": user_id}, {"$set": {"is_confirmed": True}})
    referrer_id = user.get("referrer_id")
    if referrer_id and referrer_id != user_id:
        await increment_referral(referrer_id)
        return referrer_id
    return None


async def increment_referral(referrer_id: int):
    await users_col.update_one({"user_id": referrer_id}, {"$inc": {"referral_count": 1}})


async def get_top_referrers(limit: int = 10):
    cursor = users_col.find({"referral_count": {"$gt": 0}}).sort("referral_count", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_total_users() -> int:
    return await users_col.count_documents({})


async def get_confirmed_users_count() -> int:
    return await users_col.count_documents({"is_confirmed": True})


async def get_all_user_ids():
    cursor = users_col.find({}, {"user_id": 1})
    return [doc["user_id"] async for doc in cursor]


async def reset_contest():
    """Barcha referal hisoblarini nolga tushiradi (yangi konkurs uchun)."""
    await users_col.update_many({}, {"$set": {"referral_count": 0}})


# ---------------- CHANNELS (majburiy obuna) ----------------

async def add_channel(chat_id: str, title: str, url: str):
    await channels_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"title": title, "url": url}},
        upsert=True,
    )


async def remove_channel(chat_id: str) -> bool:
    result = await channels_col.delete_one({"chat_id": chat_id})
    return result.deleted_count > 0


async def get_channels():
    cursor = channels_col.find({})
    return await cursor.to_list(length=1000)
