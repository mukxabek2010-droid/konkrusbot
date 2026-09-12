# -*- coding: utf-8 -*-
"""
Referal-konkurs Telegram boti.
Render "Web Service" + MongoDB Atlas uchun moslashtirilgan.

Kerakli muhit o'zgaruvchilari (Render -> Environment):
    BOT_TOKEN     - @BotFather dan olingan token (SHART)
    ADMIN_IDS     - 8532117429
    BOT_USERNAME  - bot username'i, @ belgisiz (SHART)
    MONGO_URI     - MongoDB ulanish manzili (SHART)
    PYTHON_VERSION - 3.11.9 (Render uchun)
    PORT          - Render avtomatik beradi, o'zingiz sozlamang
"""
import asyncio
import logging
import os
import random
from datetime import datetime

from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
)
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
LEADERBOARD_INTERVAL_SECONDS = 60  # har 1 daqiqada yangilanadi

# Bot uxlab qolmasligi uchun o'zini-o'zi "chaqirib" turadigan manzil.
# Render bu o'zgaruvchini avtomatik beradi (masalan https://konkrusbot.onrender.com).
# Agar berilmasa, SELF_URL orqali qo'lda ham ko'rsatish mumkin.
SELF_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL", "")
SELF_PING_INTERVAL_SECONDS = 4 * 60  # har 4 daqiqada

# "Fruit Value" bo'limida ochiladigan WebApp havolasi (Render -> Environment -> FRUIT_VALUE_URL)
FRUIT_VALUE_URL = os.getenv("FRUIT_VALUE_URL", "https://www.gamersberg.com/blox-fruits/calculator")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN muhit o'zgaruvchisi topilmadi! Render -> Environment bo'limida qo'shing.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

user_router = Router()
admin_router = Router()

# Fon vazifasi (har daqiqada TOP-3 ni yangilab turadi)
leaderboard_task: asyncio.Task | None = None

# Pastki menyu tugmalari matni (shu matnlarni handler'lar aniqlaydi)
BTN_STATS = "📊 Statistika"
BTN_LINK = "🔗 Referal havolam"
BTN_TOP = "🏆 Reyting"
BTN_FRUIT_VALUE = "🍓 Fruit Value"
BTN_BLOX_SERVICES = "🛠 Blox Fruit Xizmatlar"
BTN_DISCORD = "💬 Discord"
DISCORD_INVITE_URL = "https://discord.gg/fsWEG8SBZ"


class AdminStates(StatesGroup):
    waiting_broadcast_text = State()
    waiting_channel_data = State()
    waiting_gift_target = State()
    waiting_gift_amount = State()


class Contest2States(StatesGroup):
    waiting_description = State()
    waiting_channels = State()
    waiting_winners_count = State()
    waiting_photo = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------- KLAVIATURALAR ----------------

def subscription_keyboard(channels):
    builder = InlineKeyboardBuilder()
    for ch in channels:
        builder.row(InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["url"]))
    builder.row(InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_subscription"))
    return builder.as_markup()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Pastda doimiy ko'rinadigan menyu (inline emas, pastki reply-menyu)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_LINK)],
            [KeyboardButton(text=BTN_TOP)],
            [KeyboardButton(text=BTN_FRUIT_VALUE), KeyboardButton(text=BTN_BLOX_SERVICES)],
            [KeyboardButton(text=BTN_DISCORD)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_panel_keyboard(contest_running: bool):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Umumiy statistika", callback_data="adm_stats"))
    builder.row(InlineKeyboardButton(text="📋 Kanallar ro'yxati", callback_data="adm_channels"))
    builder.row(InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="adm_add_channel"))
    builder.row(InlineKeyboardButton(text="📢 Xabar yuborish (broadcast)", callback_data="adm_broadcast"))

    if contest_running:
        builder.row(InlineKeyboardButton(text="🟢 Konkurs jarayonda (to'xtatish)", callback_data="adm_stop_contest"))
    else:
        builder.row(InlineKeyboardButton(text="🚀 Konkursni boshlash", callback_data="adm_start_contest"))

    builder.row(InlineKeyboardButton(text="🏆 G'olibni e'lon qilish", callback_data="adm_declare_winner"))
    builder.row(InlineKeyboardButton(text="🔄 Joriy hisobni qo'lda nolga tushirish", callback_data="adm_reset"))
    builder.row(InlineKeyboardButton(text="🎉 KONKURS 2 (kanalga post + random)", callback_data="c2_menu"))
    return builder.as_markup()


def winner_count_keyboard():
    builder = InlineKeyboardBuilder()
    row1 = [InlineKeyboardButton(text=str(i), callback_data=f"winnum_{i}") for i in range(1, 6)]
    row2 = [InlineKeyboardButton(text=str(i), callback_data=f"winnum_{i}") for i in range(6, 11)]
    builder.row(*row1)
    builder.row(*row2)
    builder.row(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm_cancel"))
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
        f"{BTN_LINK} — shaxsiy havolangizni olish\n"
        f"{BTN_STATS} — sizning joriy natijangiz va TOP ishtirokchilar\n"
        f"{BTN_TOP} — umumiy (hech qachon nollanmaydigan) reyting\n\n"
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


def format_top_text(top_rows, title: str) -> str:
    if not top_rows:
        return f"{title}\n\nHozircha reytingda hech kim yo'q."
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{title}\n"]
    for i, row in enumerate(top_rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = row.get("full_name") or (f"@{row['username']}" if row.get("username") else str(row["user_id"]))
        count = row.get("referral_count") if "referral_count" in row else row.get("total_referral_count", 0)
        lines.append(f"{prefix} {name} — <b>{count}</b> ta referal")
    return "\n".join(lines)


def format_round_top_text(top_rows) -> str:
    if not top_rows:
        return "Hozircha reytingda hech kim yo'q."
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>TOP-10 (joriy konkurs turi):</b>\n"]
    for i, row in enumerate(top_rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = row.get("full_name") or (f"@{row['username']}" if row.get("username") else str(row["user_id"]))
        lines.append(f"{prefix} {name} — <b>{row['referral_count']}</b> ta referal")
    return "\n".join(lines)


def format_alltime_top_text(top_rows) -> str:
    if not top_rows:
        return "Hozircha reytingda hech kim yo'q."
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Umumiy reyting (hammavaqtgi):</b>", "<i>Faqat maqtanish uchun — bu hech qachon nollanmaydi 😉</i>\n"]
    for i, row in enumerate(top_rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = row.get("full_name") or (f"@{row['username']}" if row.get("username") else str(row["user_id"]))
        lines.append(f"{prefix} {name} — <b>{row['total_referral_count']}</b> ta referal")
    return "\n".join(lines)


def build_live_leaderboard_text(top3, finished: bool = False) -> str:
    now = datetime.now().strftime("%H:%M:%S")
    header = "🏁 <b>Konkurs yakunlandi!</b>" if finished else "🔥 <b>Konkurs jarayonda!</b> 🔥"
    lines = [header, "", "Hozirgi TOP-3:"]
    medals = ["🥇", "🥈", "🥉"]
    if not top3:
        lines.append("Hali hech kim referal yig'magan.")
    else:
        for i, row in enumerate(top3):
            name = row.get("full_name") or (f"@{row['username']}" if row.get("username") else str(row["user_id"]))
            lines.append(f"{medals[i]} {name} — <b>{row['referral_count']}</b> ta referal")
    if not finished:
        lines.append("")
        lines.append(f"🕐 Har {LEADERBOARD_INTERVAL_SECONDS} soniyada yangilanadi. Oxirgi yangilanish: {now}")
    return "\n".join(lines)


# ---------------- LIVE TOP-3 FON VAZIFASI ----------------

async def leaderboard_updater():
    """Konkurs davomida har LEADERBOARD_INTERVAL_SECONDS da TOP-3 xabarini yangilaydi."""
    try:
        while True:
            await asyncio.sleep(LEADERBOARD_INTERVAL_SECONDS)
            state = await db.get_contest_state()
            if not state.get("running"):
                break
            chat_id = state.get("chat_id")
            message_id = state.get("message_id")
            if not chat_id or not message_id:
                break
            top3 = await db.get_top_referrers(3, field="referral_count")
            text = build_live_leaderboard_text(top3, finished=False)
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            except TelegramBadRequest:
                pass
            except Exception as e:
                logger.warning(f"Leaderboard yangilashda xato: {e}")
    except asyncio.CancelledError:
        pass


def ensure_leaderboard_task_running():
    global leaderboard_task
    if leaderboard_task is None or leaderboard_task.done():
        leaderboard_task = asyncio.create_task(leaderboard_updater())


async def stop_leaderboard_task():
    global leaderboard_task
    if leaderboard_task and not leaderboard_task.done():
        leaderboard_task.cancel()
    leaderboard_task = None


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


@user_router.message(F.text == BTN_LINK)
async def btn_my_link(message: Message):
    user_id = message.from_user.id
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    user = await db.get_user(user_id)
    count = user["referral_count"] if user else 0
    text = (
        f"🔗 <b>Sizning shaxsiy referal havolangiz:</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"Joriy konkursdagi referallar soni: <b>{count}</b>\n\n"
        f"Ushbu havolani do'stlaringizga yuboring. Ular bot orqali kirib, "
        f"kanallarga obuna bo'lishsa, hisobingizga referal qo'shiladi."
    )
    await message.answer(text)


@user_router.message(F.text == BTN_STATS)
async def btn_stats(message: Message):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user:
        await message.answer("Xatolik yuz berdi, /start bosing.")
        return

    rank = await db.get_user_rank(user_id, field="referral_count")
    top10 = await db.get_top_referrers(10, field="referral_count")

    lines = [
        "📊 <b>Sizning statistikangiz (joriy konkurs):</b>\n",
        f"Referallar soni: <b>{user['referral_count']}</b>",
    ]
    lines.append(f"Reytingdagi o'rningiz: <b>{rank}</b>" if rank else "Siz hali reytingda emassiz.")
    lines.append("")
    lines.append(format_round_top_text(top10))

    await message.answer("\n".join(lines))


@user_router.message(F.text == BTN_TOP)
async def btn_alltime_top(message: Message):
    top10 = await db.get_top_referrers(10, field="total_referral_count")
    await message.answer(format_alltime_top_text(top10))


@user_router.message(F.text == BTN_FRUIT_VALUE)
async def btn_fruit_value(message: Message):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💎 Value", web_app=WebAppInfo(url=FRUIT_VALUE_URL)))
    await message.answer(
        "🍓 <b>Fruit Value</b>\n\nValuelarni bilish uchun pastdagi tugmani bosing:",
        reply_markup=builder.as_markup(),
    )


@user_router.message(F.text == BTN_BLOX_SERVICES)
async def btn_blox_services(message: Message):
    await message.answer("🛠 Bu bo'lim hozircha ta'mirlanmoqda. Tez orada qo'shiladi!")


@user_router.message(F.text == BTN_DISCORD)
async def btn_discord(message: Message):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💬 Discord'ga kirish", url=DISCORD_INVITE_URL))
    await message.answer(
        "Bizning Discord serverimizga qo'shiling! 👇",
        reply_markup=builder.as_markup(),
    )


@user_router.message(Command("top"))
async def cmd_top(message: Message):
    top10 = await db.get_top_referrers(10, field="total_referral_count")
    await message.answer(format_alltime_top_text(top10))


# ---------------- ADMIN QISMI ----------------

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    state = await db.get_contest_state()
    await message.answer("🛠 <b>Admin panel</b>", reply_markup=admin_panel_keyboard(state.get("running", False)))


# ---- YASHIRIN BUYRUQ: /referal (hech qayerda ko'rsatilmaydi, admin panelida ham yo'q) ----
# Faqat ADMIN_IDS ichidagi odam shu buyruqni bilib, yozsagina ishlaydi.
# Boshqa har qanday odam (oddiy foydalanuvchi yoki hatto boshqa admin) buni yozsa ham,
# bot sukut saqlaydi — hech qanday javob qaytmaydi, hech narsa oshkor bo'lmaydi.

@admin_router.message(Command("referal"))
async def cmd_secret_gift_referal(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_gift_target)
    await message.answer(
        "🎁 <b>Referal sovg'a bo'limi</b>\n\n"
        "Foydalanuvchining ID raqamini yoki username'ini yuboring "
        "(masalan: <code>123456789</code> yoki <code>@username</code>).\n\n"
        "Bekor qilish uchun /bekor yozing."
    )


@admin_router.message(Command("bekor"))
async def cmd_admin_cancel_any(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    current = await state.get_state()
    if current:
        await state.clear()
        await message.answer("Bekor qilindi.")


@admin_router.message(AdminStates.waiting_gift_target)
async def process_gift_target(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()
    target_user = None
    if text.lstrip("-").isdigit():
        target_user = await db.get_user(int(text))
    else:
        target_user = await db.get_user_by_username(text.lstrip("@"))

    if not target_user:
        await message.answer(
            "❌ Bunday foydalanuvchi topilmadi (u botdan kamida bir marta /start bosgan bo'lishi kerak).\n"
            "Qaytadan ID yoki username yuboring, yoki /bekor yozing."
        )
        return

    name = target_user.get("full_name") or (
        f"@{target_user['username']}" if target_user.get("username") else str(target_user["user_id"])
    )
    await state.update_data(target_user_id=target_user["user_id"], target_name=name)
    await state.set_state(AdminStates.waiting_gift_amount)
    await message.answer(
        f"👤 Topildi: <b>{name}</b>\n\n"
        f"Nechta referal sovg'a qilmoqchisiz? (1 dan 1000 gacha son kiriting):"
    )


@admin_router.message(AdminStates.waiting_gift_amount)
async def process_gift_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 1000):
        await message.answer("❌ 1 dan 1000 gacha butun son kiriting, yoki /bekor yozing.")
        return

    amount = int(text)
    data = await state.get_data()
    target_user_id = data["target_user_id"]
    target_name = data["target_name"]

    await db.add_referral_amount(target_user_id, amount)
    await state.clear()

    updated = await db.get_user(target_user_id)
    await message.answer(
        f"✅ <b>{target_name}</b> ga <b>{amount}</b> ta referal sovg'a qilindi!\n\n"
        f"Joriy tur hisobi: <b>{updated['referral_count']}</b>\n"
        f"Umumiy (hammavaqtgi) hisobi: <b>{updated['total_referral_count']}</b>"
    )

    try:
        await bot.send_message(
            target_user_id,
            f"🎁 Tabriklaymiz! Sizga sovg'a sifatida <b>{amount}</b> ta referal qo'shildi!",
        )
    except Exception:
        pass


@admin_router.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    contest_state = await db.get_contest_state()
    await callback.message.edit_text(
        "🛠 <b>Admin panel</b>",
        reply_markup=admin_panel_keyboard(contest_state.get("running", False)),
    )
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
    contest_state = await db.get_contest_state()
    running_text = "🟢 Ha, jarayonda" if contest_state.get("running") else "🔴 Yo'q"
    text = (
        f"📊 <b>Umumiy statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total}</b>\n"
        f"✅ Obunani tasdiqlaganlar: <b>{confirmed}</b>\n"
        f"📢 Majburiy kanallar soni: <b>{channels_count}</b>\n"
        f"🎯 Konkurs holati: <b>{running_text}</b>"
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


async def broadcast_message_copy(source_message: Message) -> tuple[int, int]:
    """source_message ni barcha foydalanuvchilarga nusxalab yuboradi. (sent, failed) qaytaradi."""
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await source_message.copy_to(chat_id=uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


async def broadcast_text_to_all(text: str) -> tuple[int, int]:
    """Oddiy matnli xabarni barcha foydalanuvchilarga yuboradi. (sent, failed) qaytaradi."""
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


@admin_router.message(AdminStates.waiting_broadcast_text)
async def process_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    status_msg = await message.answer("⏳ Yuborilmoqda...")
    sent, failed = await broadcast_message_copy(message)
    await status_msg.edit_text(
        f"✅ Xabar yuborish yakunlandi.\n\n✅ Yuborildi: {sent}\n❌ Xato: {failed}",
        reply_markup=back_to_admin_keyboard(),
    )


# ---- Konkursni boshlash / to'xtatish ----

@admin_router.callback_query(F.data == "adm_start_contest")
async def cb_adm_start_contest(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    top3 = await db.get_top_referrers(3, field="referral_count")
    text = build_live_leaderboard_text(top3, finished=False)
    msg = await bot.send_message(callback.message.chat.id, text)
    await db.set_contest_state(running=True, chat_id=msg.chat.id, message_id=msg.message_id)
    ensure_leaderboard_task_running()

    await callback.message.edit_text(
        "🚀 Konkurs boshlandi! TOP-3 xabari yuborildi va har "
        f"{LEADERBOARD_INTERVAL_SECONDS} soniyada avtomatik yangilanadi.",
        reply_markup=admin_panel_keyboard(True),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "adm_stop_contest")
async def cb_adm_stop_contest(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await stop_leaderboard_task()
    await db.set_contest_state(running=False)
    await callback.message.edit_text(
        "🛑 Konkurs to'xtatildi (g'olib e'lon qilinmadi, hisoblar o'zgarmadi).",
        reply_markup=admin_panel_keyboard(False),
    )
    await callback.answer()


# ---- G'olibni e'lon qilish ----

@admin_router.callback_query(F.data == "adm_declare_winner")
async def cb_adm_declare_winner(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🏆 Nechta g'olib bo'lishi kerak? (1 dan 10 gacha)\n\n"
        "Eng ko'p referal yig'gan ishtirokchilar tanlanadi.",
        reply_markup=winner_count_keyboard(),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("winnum_"))
async def cb_winnum(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    n = int(callback.data.replace("winnum_", ""))
    top_n = await db.get_top_referrers(n, field="referral_count")

    if not top_n:
        await callback.message.edit_text(
            "Hozircha g'oliblarni aniqlash uchun yetarli ma'lumot yo'q "
            "(hech kim referal yig'magan).",
            reply_markup=back_to_admin_keyboard(),
        )
        await callback.answer()
        return

    # Live-yangilanishni to'xtatamiz va yakuniy holatga o'zgartiramiz
    state = await db.get_contest_state()
    await stop_leaderboard_task()
    if state.get("chat_id") and state.get("message_id"):
        try:
            final_text = build_live_leaderboard_text(top_n[:3], finished=True)
            await bot.edit_message_text(
                chat_id=state["chat_id"], message_id=state["message_id"], text=final_text
            )
        except Exception:
            pass
    await db.clear_contest_state()

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>KONKURS G'OLIBLARI!</b> 🏆\n"]
    for i, row in enumerate(top_n):
        prefix = medals[i] if i < 3 else f"{i + 1}-o'rin"
        name = row.get("full_name") or (f"@{row['username']}" if row.get("username") else str(row["user_id"]))
        lines.append(f"{prefix} {name} — <b>{row['referral_count']}</b> ta referal")
    winners_text = "\n".join(lines) + "\n\n🎁 Tabriklaymiz! Sovg'alaringiz uchun admin bilan bog'laning."

    await callback.message.edit_text("⏳ G'oliblar e'lon qilinmoqda va barchaga yuborilmoqda...")

    sent, failed = await broadcast_text_to_all(winners_text)

    # Joriy tur hisobini nolga tushiramiz (umumiy/umrbod reyting o'zgarmaydi)
    await db.reset_current_round()

    await callback.message.edit_text(
        f"{winners_text}\n\n"
        f"📤 Xabar yuborildi: {sent} ta foydalanuvchiga (xato: {failed})\n"
        f"🔄 Joriy tur hisoblari nolga tushirildi. Umumiy reyting o'zgarmadi.",
        reply_markup=back_to_admin_keyboard(),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "adm_reset")
async def cb_adm_reset(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "⚠️ Haqiqatan ham barcha ishtirokchilarning JORIY TUR referal hisoblarini nolga "
        "tushirmoqchimisiz? (Umumiy reytingga tegmaydi). Bu amalni ortga qaytarib bo'lmaydi.",
        reply_markup=confirm_cancel_keyboard("adm_reset_confirm"),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "adm_reset_confirm")
async def cb_adm_reset_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await db.reset_current_round()
    await callback.message.edit_text(
        "✅ Joriy tur referal hisoblari nolga tushirildi. Umumiy reyting o'zgarmadi.",
        reply_markup=back_to_admin_keyboard(),
    )
    await callback.answer()


# ==================== KONKURS 2 (kanalga post + tasodifiy g'olib) ====================

def _c2_parse_channels(text: str):
    """Har qatordagi kanal linkidan chat_id (@username) va to'liq url ajratib oladi."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    channels = []
    for line in lines:
        username = (
            line.replace("https://t.me/", "")
            .replace("http://t.me/", "")
            .replace("t.me/", "")
            .lstrip("@")
            .strip()
        )
        if not username:
            continue
        url = line if line.startswith("http") else f"https://t.me/{username}"
        channels.append({"chat_id": f"@{username}", "url": url})
    return channels


@admin_router.callback_query(F.data == "c2_menu")
async def cb_c2_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    state = await db.get_contest2_state()
    builder = InlineKeyboardBuilder()

    if state.get("active"):
        participants = state.get("participants", [])
        text = (
            "🎉 <b>KONKURS 2</b>\n\n"
            "Holat: 🟢 Faol (e'lon qilingan)\n"
            f"👥 Hozirgi ishtirokchilar: <b>{len(participants)}</b>\n"
            f"🏆 G'oliblar soni: <b>{state.get('winners_count')}</b>"
        )
        builder.row(InlineKeyboardButton(text="🛑 To'xtatish (g'olibni aniqlash)", callback_data="c2_stop"))
    else:
        text = (
            "🎉 <b>KONKURS 2</b>\n\n"
            "Holat: 🔴 Faol emas\n\n"
            "Kanalga rasm/matn bilan e'lon qilinadigan, ishtirokchilar orasidan "
            "TASODIFIY g'olib(lar) tanlanadigan konkurs yaratish uchun tugmani bosing."
        )
        builder.row(InlineKeyboardButton(text="🚀 Yangi konkurs yaratish", callback_data="c2_create"))

    builder.row(InlineKeyboardButton(text="⬅️ Admin panelga qaytish", callback_data="adm_back"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@admin_router.callback_query(F.data == "c2_create")
async def cb_c2_create(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(Contest2States.waiting_description)
    await callback.message.edit_text(
        "📝 <b>1/4</b> — Konkurs matnini (tavsifini, o'zbek tilida) yuboring:\n\n"
        "Masalan: «🎉 Yangi konkurs! Sovg'a — 50000 so'm balans. Ishtirok etish uchun "
        "kanallarga obuna bo'ling va pastdagi tugmani bosing!»",
        reply_markup=back_to_admin_keyboard(),
    )
    await callback.answer()


@admin_router.message(Contest2States.waiting_description)
async def c2_process_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(description=message.text)
    await state.set_state(Contest2States.waiting_channels)
    await message.answer(
        "📢 <b>2/4</b> — Majburiy obuna kanal(lar) linkini yuboring.\n"
        "Bir nechta bo'lsa, har birini alohida qatorga yozing.\n\n"
        "Masalan:\n<code>https://t.me/kanal1\nhttps://t.me/kanal2</code>\n\n"
        "⚠️ Bot ushbu kanal(lar)da <b>admin</b> bo'lishi shart — chunki post shu yerga "
        "yuboriladi va obuna shu yerdan tekshiriladi."
    )


@admin_router.message(Contest2States.waiting_channels)
async def c2_process_channels(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    channels = _c2_parse_channels(message.text)
    if not channels:
        await message.answer("❌ Kamida bitta to'g'ri kanal linki yuboring.")
        return
    await state.update_data(channels=channels)
    await state.set_state(Contest2States.waiting_winners_count)
    await message.answer("🏆 <b>3/4</b> — Nechta odam g'olib bo'ladi? (son kiriting):")


@admin_router.message(Contest2States.waiting_winners_count)
async def c2_process_winners_count(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ Musbat butun son kiriting (masalan: 1, 2, 3...).")
        return
    await state.update_data(winners_count=int(text))
    await state.set_state(Contest2States.waiting_photo)
    await message.answer(
        "🖼 <b>4/4</b> — Rasm yuboring (ixtiyoriy).\n"
        "Agar rasmsiz, faqat matn bilan e'lon qilmoqchi bo'lsangiz — /skip deb yozing."
    )


async def _c2_finalize(message: Message, state: FSMContext, photo_file_id: str | None):
    data = await state.get_data()
    description = data["description"]
    channels = data["channels"]
    winners_count = data["winners_count"]
    await state.clear()

    lines = [description, "", "📢 <b>Majburiy obuna:</b>"]
    for ch in channels:
        lines.append(f"• {ch['url']}")
    lines.append("")
    lines.append(f"🏆 G'oliblar soni: <b>{winners_count}</b>")
    lines.append("\nQatnashish uchun pastdagi <b>«🎉 Ishtirok etish»</b> tugmasini bosing!")
    caption = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    for ch in channels:
        builder.row(InlineKeyboardButton(text=f"📢 {ch['chat_id']}", url=ch["url"]))
    builder.row(InlineKeyboardButton(text="🎉 Ishtirok etish", callback_data="c2_join"))
    markup = builder.as_markup()

    posts = []
    failed_channels = []
    for ch in channels:
        try:
            if photo_file_id:
                sent = await bot.send_photo(ch["chat_id"], photo=photo_file_id, caption=caption, reply_markup=markup)
            else:
                sent = await bot.send_message(ch["chat_id"], caption, reply_markup=markup)
            posts.append({"chat_id": sent.chat.id, "message_id": sent.message_id, "has_photo": bool(photo_file_id)})
        except Exception as e:
            failed_channels.append(ch["chat_id"])
            logger.warning(f"Konkurs 2 postini {ch['chat_id']} ga yuborishda xato: {e}")

    await db.set_contest2_state(
        active=True,
        description=description,
        channels=channels,
        winners_count=winners_count,
        photo_file_id=photo_file_id,
        posts=posts,
        participants=[],
    )

    status = f"✅ Konkurs 2 e'lon qilindi! {len(posts)} ta kanalga yuborildi."
    if failed_channels:
        status += (
            f"\n⚠️ Quyidagi kanal(lar)ga yubora olmadim — bot u yerda admin emasligi mumkin: "
            f"{', '.join(failed_channels)}"
        )
    await message.answer(status, reply_markup=back_to_admin_keyboard())


@admin_router.message(Contest2States.waiting_photo, F.photo)
async def c2_process_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await _c2_finalize(message, state, message.photo[-1].file_id)


@admin_router.message(Contest2States.waiting_photo, Command("skip"))
async def c2_skip_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await _c2_finalize(message, state, None)


@admin_router.message(Contest2States.waiting_photo)
async def c2_photo_fallback(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🖼 Rasm yuboring, yoki rasmsiz o'tkazib yuborish uchun /skip deb yozing.")


@user_router.callback_query(F.data == "c2_join")
async def cb_c2_join(callback: CallbackQuery):
    state = await db.get_contest2_state()
    if not state.get("active"):
        await callback.answer("❌ Bu konkurs hozircha faol emas yoki allaqachon yakunlangan.", show_alert=True)
        return

    user_id = callback.from_user.id
    if await db.is_contest2_participant(user_id):
        await callback.answer("✅ Siz allaqachon ishtirok etyapsiz!", show_alert=True)
        return

    not_subscribed = []
    for ch in state.get("channels", []):
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
            if member.status in ("left", "kicked"):
                not_subscribed.append(ch)
        except Exception:
            not_subscribed.append(ch)

    if not_subscribed:
        await callback.answer("❌ Avval barcha ko'rsatilgan kanallarga obuna bo'ling!", show_alert=True)
        return

    await db.add_contest2_participant(
        user_id, callback.from_user.full_name, callback.from_user.username or ""
    )
    await callback.answer("🎉 Siz konkursda ishtirok etyapsiz! Omad tilaymiz!", show_alert=True)


@admin_router.callback_query(F.data == "c2_stop")
async def cb_c2_stop(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    state = await db.get_contest2_state()
    participants = state.get("participants", [])
    winners_count = state.get("winners_count", 1)

    if not participants:
        await callback.message.edit_text(
            "❌ Hozircha hech kim ishtirok etmagan, g'olib aniqlab bo'lmaydi.",
            reply_markup=back_to_admin_keyboard(),
        )
        await callback.answer()
        return

    n = min(winners_count, len(participants))
    winners = random.sample(participants, n)

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🎉 <b>KONKURS YAKUNLANDI!</b> 🎉", "", "🏆 <b>G'oliblar:</b>"]
    for i, w in enumerate(winners):
        prefix = medals[i] if i < 3 else f"{i + 1}-o'rin"
        mention = f"<a href='tg://user?id={w['user_id']}'>{w['name']}</a>"
        lines.append(f"{prefix} {mention}")
    lines.append("")
    lines.append("🎁 Tabriklaymiz! Sovg'alaringiz uchun admin bilan bog'laning.")
    result_text = "\n".join(lines)

    for post in state.get("posts", []):
        try:
            if post.get("has_photo"):
                await bot.edit_message_caption(
                    chat_id=post["chat_id"], message_id=post["message_id"], caption=result_text
                )
            else:
                await bot.edit_message_text(
                    chat_id=post["chat_id"], message_id=post["message_id"], text=result_text
                )
        except Exception as e:
            logger.warning(f"Konkurs 2 postini yangilashda xato: {e}")
            try:
                await bot.send_message(post["chat_id"], result_text)
            except Exception:
                pass

    await db.reset_contest2()

    await callback.message.edit_text(
        f"{result_text}\n\n✅ Natija tegishli kanal(lar)ga e'lon qilindi.",
        reply_markup=back_to_admin_keyboard(),
    )
    await callback.answer()


# ---------------- RENDER UCHUN HTTP SERVER (health-check) ----------------

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


async def self_ping_loop():
    """
    Render'ning bepul tarifi ~15 daqiqa harakatsizlikdan keyin servisni "uxlatib" qo'yadi.
    Buning oldini olish uchun bot har SELF_PING_INTERVAL_SECONDS da o'zining ochiq
    HTTP manziliga so'rov yuborib turadi.
    """
    if not SELF_URL:
        logger.warning(
            "SELF_URL/RENDER_EXTERNAL_URL topilmadi — self-ping o'chirilgan. "
            "Render Environment'da avtomatik berilishi kerak, aks holda SELF_URL ni qo'lda qo'shing."
        )
        return

    url = SELF_URL.rstrip("/") + "/"
    timeout = ClientTimeout(total=15)

    while True:
        await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    logger.info(f"Self-ping: {url} -> {resp.status}")
        except Exception as e:
            logger.warning(f"Self-ping xatosi: {e}")


# ---------------- ISHGA TUSHIRISH ----------------

async def main():
    await db.init_db()
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await run_http_server()
    asyncio.create_task(self_ping_loop())

    # Agar bot qayta ishga tushgan bo'lsa va konkurs "running" holatda qolgan bo'lsa,
    # live-yangilanishni davom ettiramiz.
    state = await db.get_contest_state()
    if state.get("running") and state.get("chat_id") and state.get("message_id"):
        ensure_leaderboard_task_running()
        logger.info("Konkurs live-yangilanishi davom ettirildi.")

    logger.info("Bot ishga tushdi (polling rejimida).")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
