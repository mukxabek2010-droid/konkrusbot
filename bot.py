# -*- coding: utf-8 -*-
"""
Referal-konkurs Telegram boti.
Render "Web Service" + MongoDB Atlas uchun moslashtirilgan.

Kerakli muhit o'zgaruvchilari (Render -> Environment):
    BOT_TOKEN     - 8842754251:AAEe-w4OUzSc0CJip0KVdrwIBq_xW9D6xUo
    ADMIN_IDS     - 8866852203
    BOT_USERNAME  - Bloxfruitkonkurs_bot
    MONGO_URI     - MongoDB ulanish manzili (ixtiyoriy, kodda standart qiymat bor)
    PORT          - Render avtomatik beradi, o'zingiz sozlamang
"""
import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import database as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- SOZLAMALAR (muhit o'zgaruvchilaridan) ----------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN muhit o'zgaruvchisi topilmadi! Render -> Environment bo'limida qo'shing.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

user_router = Router()
admin_router = Router()


class AdminStates(StatesGroup):
    waiting_broadcast_text = State()
    waiting_channel_data = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------- KLAVIATURALAR ----------------

def subscription_keyboard(channels):
    builder = InlineKeyboardBuilder()
    for ch in channels:
        builder.row(InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["url"]))
    builder.row(InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_subscription"))
    return builder.as_markup()


def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Referal havolam", callback_data="my_link"))
    builder.row(InlineKeyboardButton(text="🏆 Reyting (TOP-10)", callback_data="top_list"))
    builder.row(InlineKeyboardButton(text="📊 Mening statistikam", callback_data="my_stats"))
    return builder.as_markup()


def admin_panel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Umumiy statistika", callback_data="adm_stats"))
    builder.row(InlineKeyboardButton(text="📋 Kanallar ro'yxati", callback_data="adm_channels"))
    builder.row(InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="adm_add_channel"))
    builder.row(InlineKeyboardButton(text="📢 Xabar yuborish (broadcast)", callback_data="adm_broadcast"))
    builder.row(InlineKeyboardButton(text="🏆 G'oliblarni e'lon qilish", callback_data="adm_winners"))
    builder.row(InlineKeyboardButton(text="🔄 Konkursni qayta boshlash", callback_data="adm_reset"))
    return builder.as_markup()


def confirm_cancel_keyboard(confirm_data: str, cancel_data: str = "adm_cancel"):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Ha, tasdiqlayman", callback_data=confirm_data),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data=cancel_data),
    )
    return builder.as_markup()


def back_to_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Admin panelga qaytish", callback_data="adm_back"))
    return builder.as_markup()


# ---------------- YORDAMCHI FUNKSIYALAR ----------------

async def check_user_subscription(user_id: int):
    channels = await db.get_channels()
    if not channels:
        return True, []

    not_subscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
            if member.status in ("left", "kicked"):
                not_subscribed.append(ch)
        except TelegramBadRequest:
            not_subscribed.append(ch)
        except Exception as e:
            logger.warning(f"Obunani tekshirishda xato ({ch['chat_id']}): {e}")
            not_subscribed.append(ch)

    return len(not_subscribed) == 0, not_subscribed


async def send_subscription_prompt(chat_id: int, not_subscribed_channels):
    text = (
        "🎉 <b>Konkursda ishtirok etish uchun</b> quyidagi kanal(lar)ga obuna bo'ling, "
        "so'ngra pastdagi <b>«Obunani tekshirish»</b> tugmasini bosing:"
    )
    await bot.send_message(chat_id, text, reply_markup=subscription_keyboard(not_subscribed_channels))


async def send_welcome(chat_id: int):
    text = (
        "👋 Xush kelibsiz!\n\n"
        "Bu konkurs-bot orqali siz do'stlaringizni taklif qilib, sovg'alar yutish imkoniyatiga ega bo'lasiz.\n\n"
        "🔗 <b>Referal havolam</b> — shaxsiy havolangizni olish\n"
        "🏆 <b>Reyting</b> — eng ko'p referal yig'gan ishtirokchilar\n"
        "📊 <b>Mening statistikam</b> — sizning natijangiz\n\n"
        "Do'stingiz sizning havolangiz orqali botga kirib, barcha kanallarga obuna bo'lsa, "
        "sizning hisobingizga +1 referal qo'shiladi. Omad!"
    )
    await bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())


async def notify_referrer(referrer_id: int):
    try:
        await bot.send_message(
            referrer_id,
            "🎉 Sizning referal havolangiz orqali yangi ishtirokchi qo'shildi! "
            "Hisobingizga +1 referal qo'shildi."
        )
    except Exception:
        pass


def format_top_text(top_rows) -> str:
    if not top_rows:
        return "Hozircha reytingda hech kim yo'q."
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>TOP-10 ishtirokchilar:</b>\n"]
    for i, row in enumerate(top_rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = row.get("full_name") or (f"@{row['username']}" if row.get("username") else str(row["user_id"]))
        lines.append(f"{prefix} {name} — <b>{row['referral_count']}</b> ta referal")
    return "\n".join(lines)


# ---------------- FOYDALANUVCHI QISMI ----------------

@user_router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    args = message.text.split(maxsplit=1)
    referrer_id = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_candidate = int(args[1].replace("ref_", ""))
            if ref_candidate != user.id:
                referrer_id = ref_candidate
        except ValueError:
            referrer_id = None

    await db.add_user(user.id, user.username or "", user.full_name, referrer_id)

    subscribed, not_subscribed = await check_user_subscription(user.id)
    if not subscribed:
        await send_subscription_prompt(message.chat.id, not_subscribed)
        return

    ref_awarded_to = await db.confirm_user(user.id)
    if ref_awarded_to:
        await notify_referrer(ref_awarded_to)

    await send_welcome(message.chat.id)


@user_router.callback_query(F.data == "check_subscription")
async def cb_check_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    subscribed, not_subscribed = await check_user_subscription(user_id)

    if not subscribed:
        await callback.answer("❌ Siz hali barcha kanallarga obuna bo'lmagansiz!", show_alert=True)
        return

    ref_awarded_to = await db.confirm_user(user_id)
    if ref_awarded_to:
        await notify_referrer(ref_awarded_to)

    await callback.message.delete()
    await callback.answer("✅ Obuna tasdiqlandi!")
    await send_welcome(callback.message.chat.id)


@user_router.callback_query(F.data == "my_link")
async def cb_my_link(callback: CallbackQuery):
    user_id = callback.from_user.id
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    user = await db.get_user(user_id)
    count = user["referral_count"] if user else 0
    text = (
        f"🔗 <b>Sizning shaxsiy referal havolangiz:</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"Hozirgi referallar soni: <b>{count}</b>\n\n"
        f"Ushbu havolani do'stlaringizga yuboring. Ular bot orqali kirib, "
        f"kanallarga obuna bo'lishsa, hisobingizga referal qo'shiladi."
    )
    await callback.message.answer(text)
    await callback.answer()


@user_router.callback_query(F.data == "top_list")
async def cb_top_list(callback: CallbackQuery):
    top = await db.get_top_referrers(10)
    await callback.message.answer(format_top_text(top))
    await callback.answer()


@user_router.callback_query(F.data == "my_stats")
async def cb_my_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if not user:
        await callback.answer("Xatolik yuz berdi, /start bosing.", show_alert=True)
        return

    top = await db.get_top_referrers(100000)
    rank = next((i + 1 for i, row in enumerate(top) if row["user_id"] == user_id), None)

    text = f"📊 <b>Sizning statistikangiz:</b>\n\nReferallar soni: <b>{user['referral_count']}</b>\n"
    text += f"Reytingdagi o'rningiz: <b>{rank}</b>" if rank else "Siz hali reytingda emassiz."
    await callback.message.answer(text)
    await callback.answer()


@user_router.message(Command("top"))
async def cmd_top(message: Message):
    top = await db.get_top_referrers(10)
    await message.answer(format_top_text(top))


# ---------------- ADMIN QISMI ----------------

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Admin panel</b>", reply_markup=admin_panel_keyboard())


@admin_router.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text("🛠 <b>Admin panel</b>", reply_markup=admin_panel_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "adm_cancel")
async def cb_adm_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text("Bekor qilindi.", reply_markup=back_to_admin_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "adm_stats")
async def cb_adm_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    total = await db.get_total_users()
    confirmed = await db.get_confirmed_users_count()
    channels_count = len(await db.get_channels())
    text = (
        f"📊 <b>Umumiy statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total}</b>\n"
        f"✅ Obunani tasdiqlaganlar: <b>{confirmed}</b>\n"
        f"📢 Majburiy kanallar soni: <b>{channels_count}</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_to_admin_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "adm_channels")
async def cb_adm_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    channels = await db.get_channels()
    if not channels:
        text = "Hozircha majburiy obuna kanallari qo'shilmagan."
    else:
        lines = ["📋 <b>Majburiy obuna kanallari:</b>\n"]
        for ch in channels:
            lines.append(f"• {ch['title']} — <code>{ch['chat_id']}</code>\n  {ch['url']}")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=back_to_admin_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "adm_add_channel")
async def cb_adm_add_channel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_channel_data)
    text = (
        "➕ <b>Kanal qo'shish</b>\n\n"
        "Quyidagi formatda yuboring (har biri alohida qatorda):\n\n"
        "<code>chat_id\nNomi\nhttps://t.me/kanal_username</code>\n\n"
        "Masalan:\n"
        "<code>@mychannel\nMening kanalim\nhttps://t.me/mychannel</code>\n\n"
        "⚠️ Diqqat: bot ushbu kanalda <b>admin</b> bo'lishi shart, aks holda obunani tekshira olmaydi."
    )
    await callback.message.edit_text(text, reply_markup=back_to_admin_keyboard())
    await callback.answer()


@admin_router.message(AdminStates.waiting_channel_data)
async def process_add_channel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    parts = [p.strip() for p in message.text.split("\n") if p.strip()]
    if len(parts) < 3:
        await message.answer(
            "❌ Format xato. Iltimos, 3 qatorda yuboring:\nchat_id\nNomi\nHavola",
            reply_markup=back_to_admin_keyboard(),
        )
        return

    chat_id, title, url = parts[0], parts[1], parts[2]
    await db.add_channel(chat_id, title, url)
    await state.clear()
    await message.answer(
        f"✅ Kanal qo'shildi: <b>{title}</b> ({chat_id})",
        reply_markup=back_to_admin_keyboard(),
    )


@admin_router.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_broadcast_text)
    await callback.message.edit_text(
        "📢 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yozing:",
        reply_markup=back_to_admin_keyboard(),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_broadcast_text)
async def process_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ Yuborilmoqda... (0/{len(user_ids)})")

    for i, uid in enumerate(user_ids, 1):
        try:
            await message.copy_to(chat_id=uid)
            sent += 1
        except Exception:
            failed += 1
        if i % 25 == 0:
            try:
                await status_msg.edit_text(f"⏳ Yuborilmoqda... ({i}/{len(user_ids)})")
            except Exception:
                pass
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ Xabar yuborish yakunlandi.\n\n✅ Yuborildi: {sent}\n❌ Xato: {failed}",
        reply_markup=back_to_admin_keyboard(),
    )


@admin_router.callback_query(F.data == "adm_winners")
async def cb_adm_winners(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    top2 = await db.get_top_referrers(2)
    if not top2:
        await callback.message.edit_text(
            "Hozircha g'oliblarni aniqlash uchun yetarli ma'lumot yo'q.",
            reply_markup=back_to_admin_keyboard(),
        )
        await callback.answer()
        return

    medals = ["🥇", "🥈"]
    winners_lines = ["🏆 <b>KONKURS G'OLIBLARI!</b> 🏆\n"]
    for i, row in enumerate(top2):
        name = row.get("full_name") or (f"@{row['username']}" if row.get("username") else str(row["user_id"]))
        winners_lines.append(f"{medals[i]} {name} — <b>{row['referral_count']}</b> ta referal")

    winners_text = "\n".join(winners_lines) + "\n\n🎁 Tabriklaymiz! Sovg'alaringiz uchun admin bilan bog'laning."

    await callback.message.edit_text(
        "\n".join(winners_lines) + "\n\nUshbu e'lonni barcha foydalanuvchilarga yuborish uchun "
        "\"📢 Xabar yuborish\" bo'limidan foydalanib, shu matnni joylashtiring:\n\n"
        f"<code>{winners_text}</code>",
        reply_markup=back_to_admin_keyboard(),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "adm_reset")
async def cb_adm_reset(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "⚠️ Haqiqatan ham barcha ishtirokchilarning referal hisoblarini nolga tushirmoqchimisiz? "
        "Bu amalni ortga qaytarib bo'lmaydi.",
        reply_markup=confirm_cancel_keyboard("adm_reset_confirm"),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "adm_reset_confirm")
async def cb_adm_reset_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await db.reset_contest()
    await callback.message.edit_text(
        "✅ Konkurs qayta boshlandi. Barcha referal hisoblari nolga tushirildi.",
        reply_markup=back_to_admin_keyboard(),
    )
    await callback.answer()


# ---------------- RENDER UCHUN HTTP SERVER (health-check) ----------------
# Render "Web Service" muhitida ilova $PORT portini tinglashi shart, aks holda
# deploy "muvaffaqiyatsiz" deb belgilanadi. Shu sababli botni polling rejimida
# ishlatib, yonida shu kichik HTTP serverni ham ko'taramiz.

async def health(request):
    return web.Response(text="Bot ishlayapti ✅")


async def run_http_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"HTTP server {PORT}-portda ishga tushdi (Render health-check uchun).")


# ---------------- ISHGA TUSHIRISH ----------------

async def main():
    await db.init_db()
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await run_http_server()

    logger.info("Bot ishga tushdi (polling rejimida).")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
