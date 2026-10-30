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
# Значения подтягиваются из Environment Variables сервера (Render)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MASTER_ID_RAW = os.getenv("MASTER_TELEGRAM_ID", "0")

# Проверка обязательных переменных окружения перед запуском
if not TELEGRAM_BOT_TOKEN:
    sys.exit("КРИТИЧЕСКАЯ ОШИБКА: TELEGRAM_BOT_TOKEN не задан в переменных окружения!")

if not GEMINI_API_KEY:
    sys.exit("КРИТИЧЕСКАЯ ОШИБКА: GEMINI_API_KEY не задан в переменных окружения!")

MASTER_TELEGRAM_ID = int(MASTER_ID_RAW) if MASTER_ID_RAW.isdigit() else 0

GEMINI_MODEL = "gemini-3.5-flash-lite"
EXCEL_FILE = "Записи_Ногти_Деревня.xlsx"
PORT = int(os.getenv("PORT", 8080))
# ========================================================

# Слоты на 25 и 26 ноября с шагом 3 часа (с 08:00 до 22:00)
BASE_SLOTS = [
    # 25 ноября
    "25 ноября 08:00",
    "25 ноября 11:00",
    "25 ноября 14:00",
    "25 ноября 17:00",
    "25 ноября 20:00",
    # 26 ноября
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


# --- 1. SQLite: защита от повторных и параллельных записей ---
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


# --- 2. Сохранение в Excel ---
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


# --- 3. Динамические клавиатуры со свободными местами ---
def get_available_slots_keyboard():
    booked = get_booked_slots()
    buttons = []
    for slot in BASE_SLOTS:
        if slot not in booked:
            buttons.append([InlineKeyboardButton(text=f"🟢 {slot}", callback_data=f"slot:{slot}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def get_services_keyboard():
    buttons