import asyncio
from datetime import datetime
import os
import sqlite3
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from google import genai
from openpyxl import Workbook, load_workbook

# ================= БЕЗОПАСНЫЕ НАСТРОЙКИ =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MASTER_ID_RAW = os.getenv("MASTER_TELEGRAM_ID", "0")

if not TELEGRAM_BOT_TOKEN:
    sys.exit("ОШИБКА: TELEGRAM_BOT_TOKEN не задан!")

if not GEMINI_API_KEY:
    sys.exit("ОШИБКА: GEMINI_API_KEY не задан!")

MASTER_TELEGRAM_ID = int(MASTER_ID_RAW) if MASTER_ID_RAW.isdigit() else 0

GEMINI_MODEL = "gemini-3.5-flash-lite"
EXCEL_FILE = "Записи_Ногти_Деревня.xlsx"
PORT = int(os.getenv("PORT", 10000))
# ========================================================

BASE_SLOTS = [
    "25 ноября 08:00",
    "25 ноября 11:00",
    "25 ноября 14:00",
    "25 ноября 17:00",
    "25 ноября 20:00",
    "26 ноября 08:00",
    "26 ноября 11:00",
    "26 ноября 14:00",
    "26 ноября 17:00",
    "26 ноября 20:00",
]

SERVICES = {
    "srv_1": ("Маникюр без покрытия", "1 000 ₽"),
    "srv_2": ("Маникюр + Гель-лак", "1 800 ₽"),
    "srv_3": ("Наращивание ногтей", "2 800 ₽"),
    "srv_4": ("Снятие + Укрепление + Дизайн", "2 300 ₽"),
}

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY)


def init_db():
    conn = sqlite3.connect("nails.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            client_name TEXT,
            phone TEXT,
            service TEXT,
            date_time TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_booked_slots():
    conn = sqlite3.connect("nails.db")
    cursor = conn.cursor()
    cursor.execute("SELECT date_time FROM appointments")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def save_appointment(user_id, name, phone, service, date_time):
    conn = sqlite3.connect("nails.db")
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO appointments (user_id, client_name, phone, service, date_time)
            VALUES (?, ?, ?, ?, ?)
        """,
            (user_id, name, phone, service, date_time),
        )
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    finally:
        conn.close()
    return success


def save_to_excel(name, phone, service, chosen_time):
    if not os.path.exists(EXCEL_FILE):
        wb = Workbook()
        ws = wb.active
        ws.title = "Записи"
        ws.append(["Дата оформления", "Имя клиента", "Телефон", "Услуга", "Время визита"])
        wb.save(EXCEL_FILE)

    wb = load_workbook(EXCEL_FILE)
    ws = wb.active
    ws.append([
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        name,
        phone,
        service,
        chosen_time,
    ])
    wb.save(EXCEL_FILE)


def get_available_slots_keyboard():
    booked = get_booked_slots()
    buttons = []
    for slot in BASE_SLOTS:
        if slot not in booked:
            buttons.append([InlineKeyboardButton(text=f"🟢 {slot}", callback_data=f"slot:{slot}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def get_services_keyboard():
    buttons = [
        [InlineKeyboardButton(text=f"{title} — {price}", callback_data=f"srv:{s_id}")]
        for s_id, (title, price) in SERVICES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💅 Записаться на ногти")],
            [KeyboardButton(text="📋 Прайс-лист"), KeyboardButton(text="❓ Вопрос мастеру")],
        ],
        resize_keyboard=True,
    )


class Booking(StatesGroup):
    choosing_service = State()
    choosing_slot = State()
    entering_phone = State()


@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Здравствуйте, {message.from_user.first_name}! 🌸\n\n"
        "Открыта запись на приезд мастера в деревню на **25 и 26 ноября**.\n"
        "График работы: с 08:00 до 22:00, шаг 3 часа.\n\n"
        "Нажмите кнопку внизу для выбора свободного окна.",
        reply_markup=get_main_menu(),
        parse_mode="Markdown",
    )


@dp.message(F.text == "📋 Прайс-лист")
async def show_price(message: types.Message):
    text = "💅 **Прайс-лист на выезд 25–26 ноября:**\n\n"
    for title, price in SERVICES.values():
        text += f"• {title} — **{price}**\n"
    text += "\n*Все инструменты проходят стерилизацию по СанПиН.*"
    await message.answer(text, parse_mode="Markdown")


@dp.message(F.text == "💅 Записаться на ногти")
async def start_booking(message: types.Message, state: FSMContext):
    await state.set_state(Booking.choosing_service)
    await message.answer("Выберите услугу:", reply_markup=get_services_keyboard())


@dp.callback_query(Booking.choosing_service, F.data.startswith("srv:"))
async def on_service_selected(callback: types.CallbackQuery, state: FSMContext):
    srv_key = callback.data.replace("srv:", "")
    service_name, price = SERVICES[srv_key]
    await state.update_data(service=f"{service_name} ({price})")

    keyboard = get_available_slots_keyboard()
    if not keyboard:
        await callback.message.edit_text("😔 К сожалению, все места на 25–26 ноября уже заняты!")
        await state.clear()
        return

    await state.set_state(Booking.choosing_slot)
    await callback.message.edit_text(
        f"Выбрано: **{service_name}**\n\nВыберите свободное время визита:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(Booking.choosing_slot, F.data.startswith("slot:"))
async def on_slot_selected(callback: types.CallbackQuery, state: FSMContext):
    slot_time = callback.data.replace("slot:", "")

    if slot_time in get_booked_slots():
        await callback.answer("⚠️ Это время уже занято другим клиентом!", show_alert=True)
        kb = get_available_slots_keyboard()
        if kb:
            await callback.message.edit_text("Выберите другое свободное время:", reply_markup=kb)
        else:
            await callback.message.edit_text("Свободных мест больше нет.")
            await state.clear()
        return

    await state.update_data(chosen_slot=slot_time)
    await state.set_state(Booking.entering_phone)

    phone_btn = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await callback.message.delete()
    await callback.message.answer(
        f"Вы выбрали время: **{slot_time}**\n\n"
        "Отправьте номер телефона для связи (нажмите кнопку внизу или напишите его в чат):",
        reply_markup=phone_btn,
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message(Booking.entering_phone)
async def on_phone_received(message: types.Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
        client_name = message.contact.first_name or message.from_user.first_name
    else:
        phone = message.text.strip()
        client_name = message.from_user.first_name

    data = await state.get_data()
    service = data.get("service")
    slot = data.get("chosen_slot")

    is_saved = save_appointment(
        user_id=message.from_user.id,
        name=client_name,
        phone=phone,
        service=service,
        date_time=slot,
    )

    await state.clear()

    if not is_saved:
        await message.answer(
            "😔 Это время успели занять за секунду до отправки контакта. Пожалуйста, выберите другое свободное время через меню.",
            reply_markup=get_main_menu(),
        )
        return

    save_to_excel(name=client_name, phone=phone, service=service, chosen_time=slot)

    await message.answer(
        f"✅ **Запись успешно подтверждена!**\n\n"
        f"💅 Услуга: {service}\n"
        f"📅 Время: **{slot}**\n"
        f"📞 Номер: {phone}\n\n"
        "Ждем вас!",
        reply_markup=get_main_menu(),
        parse_mode="Markdown",
    )

    if MASTER_TELEGRAM_ID:
        notification = (
            f"🚨 **НОВАЯ ЗАПИСЬ НА НОГТИ!**\n\n"
            f"📅 **Время:** {slot}\n"
            f"💅 **Услуга:** {service}\n"
            f"👤 **Клиент:** {client_name} (@{message.from_user.username or 'нет'})\n"
            f"📞 **Телефон:** `{phone}`"
        )
        try:
            await bot.send_message(chat_id=MASTER_TELEGRAM_ID, text=notification, parse_mode="Markdown")
        except Exception as e:
            print(f"Ошибка отправки мастеру: {e}")


@dp.message(F.text)
async def ai_consultant(message: types.Message):
    if message.text == "❓ Вопрос мастеру":
        await message.answer("Задайте любой вопрос прямо в чат — я подскажу по процедурам и материалам.")
        return

    wait_msg = await message.answer("Секундочку...")
    prompt = (
        "Ты заботливый помощник мастера ногтевого сервиса. "
        "Мастер приезжает в деревню 25 и 26 ноября (график с 08:00 до 22:00, интервал 3 часа). "
        f"Ответь вежливо, лаконично и по делу на вопрос клиента: {message.text}"
    )
    try:
        res = ai_client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        await wait_msg.edit_text(res.text)
    except Exception:
        await wait_msg.edit_text("Чтобы записаться на процедуру, нажмите кнопку «💅 Записаться на ногти» внизу.")


async def web_health_check(request):
    return web.Response(text="Bot is running!")


async def main():
    init_db()

    # Запуск легковесного веб-сервера для Render
    app = web.Application()
    app.router.add_get("/", web_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")

    # Запуск Telegram поллинга
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
