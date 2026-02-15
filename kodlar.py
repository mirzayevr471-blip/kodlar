import asyncio
import logging
import aiosqlite
from datetime import datetime, timedelta, date
from dataclasses import dataclass
from typing import List
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.bot import DefaultBotProperties
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openpyxl import Workbook

# ================= CONFIG =================
@dataclass
class Config:
    TOKEN: str = "8333369510:AAEGzEuK537SyH-jaJtL6V3noA777orB358"
    ADMIN_ID: int = 8251830471
    CHANNEL_ID: int = -1003753254748
    CHANNEL_INVITE_LINK: str = "https://t.me/+W7Jn7biOjsYwN2Ri"
    TIMEZONE: str = "Asia/Tashkent"
    SUB_DAYS: int = 30
    WARN_DAYS: List[int] = None

    def __post_init__(self):
        if self.WARN_DAYS is None:
            self.WARN_DAYS = [3, 1]

config = Config()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
TZ = pytz.timezone(config.TIMEZONE)
scheduler = AsyncIOScheduler(timezone=TZ)

DB = "database.db"


def now_dt() -> datetime:
    return datetime.now(TZ)


def today_date() -> date:
    return now_dt().date()


def days_until(expiry_date_str: str) -> int:
    expiry = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    return (expiry - today_date()).days

# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            telegram_id INTEGER PRIMARY KEY,
            fullname TEXT,
            username TEXT,
            phone TEXT,
            status TEXT DEFAULT 'inactive',
            expiry_date DATE,
            warned_3 INTEGER DEFAULT 0,
            warned_1 INTEGER DEFAULT 0,
            total_payments INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            id INTEGER PRIMARY KEY CHECK(id=1),
            price INTEGER DEFAULT 30000
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            photo_file_id TEXT
        )
        """)

        async def ensure_columns(table_name, required_columns):
            cur = await db.execute(f"PRAGMA table_info({table_name})")
            existing = {row[1] for row in await cur.fetchall()}
            for col_name, col_def in required_columns.items():
                if col_name not in existing:
                    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_def}")

        await ensure_columns(
            "users",
            {
                "username": "username TEXT",
                "phone": "phone TEXT",
                "status": "status TEXT DEFAULT 'inactive'",
                "expiry_date": "expiry_date DATE",
                "warned_3": "warned_3 INTEGER DEFAULT 0",
                "warned_1": "warned_1 INTEGER DEFAULT 0",
                "total_payments": "total_payments INTEGER DEFAULT 0",
                "created_at": "created_at TIMESTAMP",
            },
        )
        await ensure_columns(
            "payments",
            {
                "status": "status TEXT DEFAULT 'pending'",
                "payment_date": "payment_date TIMESTAMP",
                "photo_file_id": "photo_file_id TEXT",
            },
        )
        await ensure_columns("settings", {"price": "price INTEGER DEFAULT 30000"})

        await db.execute("UPDATE users SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        await db.execute("UPDATE payments SET payment_date=CURRENT_TIMESTAMP WHERE payment_date IS NULL")
        await db.execute("INSERT OR IGNORE INTO settings(id, price) VALUES(1, 30000)")
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (user_id,))
        return await cur.fetchone()

async def get_price():
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT price FROM settings WHERE id=1")
        row = await cur.fetchone()
        return row[0] if row else 30000

# ================= KEYBOARDS =================
def main_menu(active=False):
    buttons = []
    if active:
        buttons.append([KeyboardButton(text="👤 Profil")])
        buttons.append([KeyboardButton(text="🔗 Kanal linki")])
    else:
        buttons.append([KeyboardButton(text="💳 Obuna sotib olish")])
        buttons.append([KeyboardButton(text="📱 Telefon yuborish", request_contact=True)])
    buttons.append([KeyboardButton(text="📞 Support")])
    buttons.append([KeyboardButton(text="ℹ️ Yordam")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def admin_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Statistika")],
            [KeyboardButton(text="Excel Export")],
            [KeyboardButton(text="Narxni o'zgartirish")],
            [KeyboardButton(text="Aktiv qilish")],
            [KeyboardButton(text="Aktiv emas qilish")],
            [KeyboardButton(text="Chiqish")]
        ],
        resize_keyboard=True
    )

def channel_link_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔗 Kanalga kirish", url=config.CHANNEL_INVITE_LINK)]]
    )

class Registration(StatesGroup):
    waiting_fullname = State()

# ================= START =================
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    telegram_fullname = (message.from_user.full_name or "").strip()
    username = message.from_user.username

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO users(telegram_id, fullname, username, created_at)
            VALUES(?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                fullname=COALESCE(NULLIF(users.fullname, ''), excluded.fullname),
                username=excluded.username
            """,
            (user_id, telegram_fullname, username),
        )
        await db.commit()

    user = await get_user(user_id)

    # Ask for full name (name + surname) once if not provided properly
    current_fullname = (user["fullname"] or "").strip() if user else ""
    if len(current_fullname.split()) < 2:
        await state.set_state(Registration.waiting_fullname)
        return await message.answer("Iltimos, ism va familiyangizni yuboring.Masalan: Ali Valiyev")

    price = await get_price()
    if user and user["status"] == "active" and user["expiry_date"]:
        expiry = datetime.strptime(user["expiry_date"], "%Y-%m-%d").date()
        if expiry >= today_date():
            days_left = (expiry - today_date()).days
            await message.answer(
                f"? <b>Xush kelibsiz, {current_fullname}!</b>"
                f"?? <b>Obuna ma'lumotlari:</b>"
                f"? Status: ? Aktiv"
                f"? Muddat: {expiry.strftime('%d.%m.%Y')} gacha"
                f"? {days_left} kun qoldi",
                reply_markup=main_menu(True),
            )
            await message.answer("?? <b>Kanalga kirish:</b>", reply_markup=channel_link_keyboard())
            return

    await message.answer(
        f"?? <b>Xush kelibsiz, {current_fullname}!</b>"
        f"?? <b>Obuna narxi:</b> {price:,} so'm / oy"
        "?? Obuna bo'lish uchun:"
"1. Telefon raqamingizni yuboring"
"2. To'lovni amalga oshiring"
"3. Chek fotosuratini yuboring",
        reply_markup=main_menu(False),
    )


@dp.message(Registration.waiting_fullname)
async def save_fullname(message: Message, state: FSMContext):
    fullname = (message.text or "").strip()
    if len(fullname.split()) < 2:
        return await message.answer("Iltimos, to'liq ism-familiya kiriting.Masalan: Ali Valiyev")

    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET fullname=? WHERE telegram_id=?", (fullname, message.from_user.id))
        await db.commit()

    await state.clear()
    price = await get_price()
    await message.answer(
        f"? Ma'lumot saqlandi: {fullname}\n"
        f"?? Obuna narxi: {price:,} so'm\n"
        "Telefon raqamingizni yuboring yoki obuna sotib olishni bosing.",
        reply_markup=main_menu(False),
    )

# ================= CONTACT =================
@dp.message(F.contact)
async def handle_contact(message: Message):
    user_id = message.from_user.id
    if message.contact.user_id and message.contact.user_id != user_id:
        return await message.answer("Faqat o'zingizning raqamingizni yuboring.")

    phone = message.contact.phone_number
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET phone=? WHERE telegram_id=?", (phone, user_id))
        await db.commit()
    price = await get_price()
    await message.answer(f"✅ Telefon raqam qabul qilindi!\n📱 {phone}\n💰 To'lov: {price:,} so'm\n📸 Chek fotosuratini yuboring:")

# ================= PROFILE =================
@dp.message(F.text.contains("Profil"))
async def profile(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        return await message.answer("Profil topilmadi. /start buyrug'ini yuboring.")

    status_text = {
        "active": "Aktiv",
        "inactive": "Aktiv emas",
        "expired": "Tugagan",
        "banned": "Bloklangan"
    }
    profile_text = (
        f"Shaxsiy profil\n\n"
        f"ID: {user['telegram_id']}\n"
        f"Ism: {user['fullname']}\n"
        f"Telefon: {user['phone'] or 'Yuborilmagan'}\n"
        f'Status: {status_text.get(user["status"], "Nomalum")}\n'
    )
    if user["expiry_date"]:
        expiry = datetime.strptime(user["expiry_date"], "%Y-%m-%d").date()
        days_left = (expiry - today_date()).days
        profile_text += f"Obuna: {expiry.strftime('%d.%m.%Y')} ({days_left} kun qoldi)\n"

    profile_text += f"To'lovlar: {user['total_payments']} ta"
    await message.answer(profile_text)


# ================= BUY SUBSCRIPTION =================
@dp.message(F.text.contains("Obuna sotib olish"))
async def buy_subscription(message: Message):
    price = await get_price()
    await message.answer(f"💰 Obuna narxi: {price:,} so'm\n📸 To'lov chekini yuboring:")

# ================= CHANNEL LINK =================
@dp.message(F.text.contains("Kanal linki"))
async def channel_link(message: Message):
    await message.answer("🔗 Kanalga kirish:", reply_markup=channel_link_keyboard())

# ================= SUPPORT =================
@dp.message(F.text.contains("Support"))
async def support(message: Message):
    await message.answer("📞 Support uchun admin bilan bog'laning: @M_Johongir0919")

# ================= HELP =================
@dp.message(F.text.contains("Yordam"))
async def help(message: Message):
    await message.answer(
        "ℹ️ <b>Yordam</b>\n\n"
        "Botdan foydalanish:\n"
        "1. /start - Botni ishga tushirish\n"
        "2. Telefon raqamingizni yuboring\n"
        "3. Obuna sotib olish\n"
        "4. Chek fotosuratini yuboring\n"
        "5. Admin tekshiradi va obunani faollashtiradi"
    )

# ================= PAYMENT PHOTO =================
@dp.message(F.photo)
async def handle_payment_photo(message: Message):
    user_id = message.from_user.id
    file_id = message.photo[-1].file_id
    price = await get_price()

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "INSERT INTO payments(user_id, amount, photo_file_id) VALUES(?, ?, ?)",
            (user_id, price, file_id),
        )
        payment_id = cur.lastrowid
        await db.commit()

    await message.answer("? Chek qabul qilindi. Admin tekshiradi.")

    try:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="? Tasdiqlash", callback_data=f"approve_{payment_id}")],
                [InlineKeyboardButton(text="? Rad etish", callback_data=f"reject_{payment_id}")],
            ]
        )
        await bot.send_photo(
            config.ADMIN_ID,
            file_id,
            caption=f"Yangi to'lov\nID: {payment_id}\nUser: {user_id}\nMiqdor: {price} so'm",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.exception("Admin notification send failed: %s", e)

# ================= ADMIN PANEL =================
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != config.ADMIN_ID:
        return await message.answer("❌ Siz admin emassiz.")
    await message.answer("Admin panel", reply_markup=admin_menu())

# ================= ADMIN STATS =================
@dp.message(F.text.contains("Statistika"))
async def admin_stats(message: Message):
    if message.from_user.id != config.ADMIN_ID:
        return
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE status='active'")
        active_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM payments WHERE status='pending'")
        pending_payments = (await cur.fetchone())[0]
    await message.answer(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"✅ Aktiv obunachilar: {active_users}\n"
        f"⏳ Kutilayotgan to'lovlar: {pending_payments}"
    )

# ================= ADMIN EXPORT EXCEL =================
@dp.message(F.text.contains("Excel Export"))
async def admin_export(message: Message):
    if message.from_user.id != config.ADMIN_ID:
        return

    wb = Workbook()

    users_ws = wb.active
    users_ws.title = "Users"
    users_headers = [
        "telegram_id",
        "fullname",
        "username",
        "phone",
        "status",
        "expiry_date",
        "warned_3",
        "warned_1",
        "total_payments",
        "created_at",
    ]
    users_ws.append(users_headers)

    payments_ws = wb.create_sheet("Payments")
    payment_headers = [
        "id",
        "user_id",
        "amount",
        "status",
        "payment_date",
        "photo_file_id",
    ]
    payments_ws.append(payment_headers)

    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row

        users_cur = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
        users = await users_cur.fetchall()
        for user in users:
            users_ws.append([user[h] for h in users_headers])

        payments_cur = await db.execute("SELECT * FROM payments ORDER BY id DESC")
        payments = await payments_cur.fetchall()
        for payment in payments:
            payments_ws.append([payment[h] for h in payment_headers])

    filename = "users_payments_full.xlsx"
    wb.save(filename)
    with open(filename, "rb") as f:
        await message.answer_document(f, caption="?? Barcha foydalanuvchi va to'lov ma'lumotlari")

# ================= ADMIN PENDING PAYMENTS =================
@dp.message(Command("pending"))
async def pending_payments(message: Message):
    if message.from_user.id != config.ADMIN_ID:
        return
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payments WHERE status='pending'")
        payments = await cur.fetchall()
        if not payments:
            return await message.answer("⏳ Kutilayotgan to'lovlar yo'q")
        for p in payments:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve_{p['id']}")],
                [InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_{p['id']}")]
            ])
            await bot.send_photo(message.chat.id, p['photo_file_id'], caption=f"ID: {p['id']}, User: {p['user_id']}, Amount: {p['amount']}", reply_markup=keyboard)

# ================= APPROVE PAYMENT =================
@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: CallbackQuery):
    payment_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payments WHERE id=?", (payment_id,))
        payment = await cur.fetchone()
        if not payment:
            return await callback.answer("To'lov topilmadi")
        if payment["status"] != "pending":
            return await callback.answer("Bu to'lov allaqachon ko'rib chiqilgan", show_alert=True)

        user_id = payment["user_id"]
        cur = await db.execute("SELECT expiry_date FROM users WHERE telegram_id=?", (user_id,))
        user_row = await cur.fetchone()

        base_date = today_date()
        if user_row and user_row["expiry_date"]:
            current_expiry = datetime.strptime(user_row["expiry_date"], "%Y-%m-%d").date()
            if current_expiry >= base_date:
                base_date = current_expiry

        new_expiry = base_date + timedelta(days=config.SUB_DAYS)
        await db.execute("UPDATE users SET status='active', expiry_date=?, total_payments=total_payments+1, warned_3=0, warned_1=0 WHERE telegram_id=?", (new_expiry.strftime("%Y-%m-%d"), user_id))
        await db.execute("UPDATE payments SET status='approved' WHERE id=?", (payment_id,))
        await db.commit()
    await callback.answer("✅ To'lov tasdiqlandi")
    await bot.send_message(user_id, f"✅ To'lov tasdiqlandi! Obuna {config.SUB_DAYS} kun faollashtirildi.")
    await bot.send_message(user_id, "Kanalga kirish:", reply_markup=channel_link_keyboard())
    await callback.message.delete()

# ================= REJECT PAYMENT =================
@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: CallbackQuery):
    payment_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE payments SET status='rejected' WHERE id=?", (payment_id,))
        await db.commit()
    await callback.answer("❌ To'lov rad etildi")
    await callback.message.delete()

# ================= ADMIN CHANGE PRICE =================
class ChangePrice(StatesGroup):
    waiting_for_price = State()

@dp.message(F.text.contains("Narxni o'zgartirish"))
async def change_price(message: Message, state: FSMContext):
    if message.from_user.id != config.ADMIN_ID:
        return
    await message.answer("💰 Yangi narxni kiriting (faqat raqam):")
    await state.set_state(ChangePrice.waiting_for_price)

@dp.message(ChangePrice.waiting_for_price)
async def process_price(message: Message, state: FSMContext):
    text = message.text or ""

    if "Chiqish" in text:
        await state.clear()
        user = await get_user(message.from_user.id)
        active = (
            user and
            user["status"] == "active" and
            user["expiry_date"] and
            datetime.strptime(user["expiry_date"], "%Y-%m-%d").date() >= today_date()
        )
        return await message.answer("Asosiy menyuga qaytish", reply_markup=main_menu(active))

    if "Excel Export" in text and message.from_user.id == config.ADMIN_ID:
        await state.clear()
        return await admin_export(message)

    try:
        new_price = int(text)
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE settings SET price=? WHERE id=1", (new_price,))
            await db.commit()
        await message.answer(f"? Narx {new_price:,} so'm ga o'zgartirildi.")
        await state.clear()
    except ValueError:
        await message.answer("? Noto'g'ri format. Faqat raqam kiriting yoki Chiqish tugmasini bosing.")

class ManageStatus(StatesGroup):
    waiting_active_user_id = State()
    waiting_inactive_user_id = State()


@dp.message(F.text.contains("Aktiv qilish"))
async def activate_user_prompt(message: Message, state: FSMContext):
    if message.from_user.id != config.ADMIN_ID:
        return
    await state.set_state(ManageStatus.waiting_active_user_id)
    await message.answer("Aktiv qilinadigan foydalanuvchi ID sini yuboring:")


@dp.message(ManageStatus.waiting_active_user_id)
async def activate_user(message: Message, state: FSMContext):
    if message.from_user.id != config.ADMIN_ID:
        return

    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        return await message.answer("ID noto'g'ri. Raqam yuboring.")

    new_expiry = (today_date() + timedelta(days=config.SUB_DAYS)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (user_id,))
        exists = await cur.fetchone()
        if not exists:
            await state.clear()
            return await message.answer("Bu ID bo'yicha foydalanuvchi topilmadi.")

        await db.execute(
            "UPDATE users SET status='active', expiry_date=?, warned_3=0, warned_1=0 WHERE telegram_id=?",
            (new_expiry, user_id),
        )
        await db.commit()

    await state.clear()
    await message.answer("? Foydalanuvchi aktiv qilindi.", reply_markup=admin_menu())
    try:
        await bot.send_message(user_id, "? Admin tomonidan obunangiz aktiv qilindi.")
        await bot.send_message(user_id, "?? Kanalga kirish:", reply_markup=channel_link_keyboard())
    except Exception:
        pass


@dp.message(F.text.contains("Aktiv emas qilish"))
async def deactivate_user_prompt(message: Message, state: FSMContext):
    if message.from_user.id != config.ADMIN_ID:
        return
    await state.set_state(ManageStatus.waiting_inactive_user_id)
    await message.answer("Aktiv emas qilinadigan foydalanuvchi ID sini yuboring:")


@dp.message(ManageStatus.waiting_inactive_user_id)
async def deactivate_user(message: Message, state: FSMContext):
    if message.from_user.id != config.ADMIN_ID:
        return

    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        return await message.answer("ID noto'g'ri. Raqam yuboring.")

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (user_id,))
        exists = await cur.fetchone()
        if not exists:
            await state.clear()
            return await message.answer("Bu ID bo'yicha foydalanuvchi topilmadi.")

        await db.execute(
            "UPDATE users SET status='inactive', expiry_date=NULL, warned_3=0, warned_1=0 WHERE telegram_id=?",
            (user_id,),
        )
        await db.commit()

    await state.clear()
    await message.answer("? Foydalanuvchi aktiv emas holatga o'tkazildi.", reply_markup=admin_menu())
    try:
        await bot.send_message(user_id, "? Admin tomonidan obunangiz o'chirildi.")
    except Exception:
        pass

# ================= ADMIN BACK =================
@dp.message(F.text.contains("Chiqish"))
async def admin_back(message: Message):
    if message.from_user.id != config.ADMIN_ID:
        return
    user = await get_user(message.from_user.id)
    active = (
        user and
        user["status"] == "active" and
        user["expiry_date"] and
        datetime.strptime(user["expiry_date"], "%Y-%m-%d").date() >= today_date()
    )
    await message.answer("Asosiy menyuga qaytish", reply_markup=main_menu(active))


# ================= SCHEDULER FOR WARNINGS =================
async def check_expiries():
    warn_days = config.WARN_DAYS
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE status='active' AND expiry_date IS NOT NULL")
        users = await cur.fetchall()

        for user in users:
            days_left = days_until(user["expiry_date"])
            if days_left in warn_days:
                if days_left == 3 and not user["warned_3"]:
                    await bot.send_message(user["telegram_id"], f"Obunangiz {days_left} kundan keyin tugaydi. Yangilang!")
                    await db.execute("UPDATE users SET warned_3=1 WHERE telegram_id=?", (user["telegram_id"],))
                elif days_left == 1 and not user["warned_1"]:
                    await bot.send_message(user["telegram_id"], f"Obunangiz {days_left} kundan keyin tugaydi. Yangilang!")
                    await db.execute("UPDATE users SET warned_1=1 WHERE telegram_id=?", (user["telegram_id"],))
            elif days_left < 0:
                await db.execute("UPDATE users SET status='expired' WHERE telegram_id=?", (user["telegram_id"],))

        await db.commit()


# ================= RUN BOT =================
async def main():
    await init_db()
    scheduler.add_job(check_expiries, "interval", hours=24)
    scheduler.start()
    logger.info("🤖 Bot ishga tushmoqda...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
