import os
import io
import asyncio
import logging
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from google import genai
from google.genai import types as genai_types
from google.genai.types import Part

# ----------------- КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ -----------------

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Публичный адрес на Render для самопинга
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://nails-bot-online.onrender.com")

# БЕЛЫЙ СПИСОК (Whitelist): укажите сюда Telegram ID пользователей (числа)
# Если список пустой (ALLOWED_USERS = []), бот доступен всем.
ALLOWED_USERS = []

# Системный промпт для модели
SYSTEM_INSTRUCTION = (
    "Ты — универсальный, умный и дружелюбный персональный ассистент. "
    "Отвечай структурированно, точно, без лишней воды. "
    "При необходимости форматируй ответ в Markdown."
)

# Инициализация клиентов
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Локальная история диалогов в оперативной памяти: {user_id: [список сообщений]}
# Ограничиваем последними 10 репликами, чтобы не раздувать токены
chat_histories = {}
MAX_HISTORY_MESSAGES = 10

# ----------------- КЛАВИАТУРА -----------------

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧹 Очистить контекст")]
    ],
    resize_keyboard=True
)

# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------

def is_user_allowed(user_id: int) -> bool:
    """Проверка наличия пользователя в белом списке."""
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

async def call_gemini_with_retry(contents, retries=3, delay=3):
    """Вызов API Gemini с автоматическим повтором при перегрузке (ошибка 429)."""
    for attempt in range(retries):
        try:
            response = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.7,
                )
            )
            return response.text
        except Exception as e:
            err_msg = str(e).lower()
            if ("429" in err_msg or "resource_exhausted" in err_msg) and attempt < retries - 1:
                logger.warning(f"Превышен лимит запросов (429). Повтор через {delay} сек... (Попытка {attempt + 1})")
                await asyncio.sleep(delay)
                continue
            raise e

# ----------------- ОБРАБОТЧИКИ СООБЩЕНИЙ -----------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not is_user_allowed(message.from_user.id):
        await message.answer("Извините, доступ к этому ассистенту ограничен владельцем.")
        return

    chat_histories[message.from_user.id] = []
    welcome_text = (
        "Привет! Я персональный ИИ-ассистент на базе Gemini.\n\n"
        "Чем я могу помочь:\n"
        "• Отвечать на любые текстовые вопросы и писать код\n"
        "• Принимать и слушать голосовые сообщения\n"
        "• Анализировать прикрепленные фотографии и документы\n\n"
        "Нажмите кнопку «🧹 Очистить контекст», чтобы начать новую тему."
    )
    await message.answer(welcome_text, reply_markup=main_keyboard)

@dp.message(F.text == "🧹 Очистить контекст")
@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    if not is_user_allowed(message.from_user.id):
        return
    chat_histories[message.from_user.id] = []
    await message.answer("Контекст диалога очищен. Готов к новой задаче!", reply_markup=main_keyboard)

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    """Обработка голосовых сообщений."""
    if not is_user_allowed(message.from_user.id):
        await message.answer("Доступ ограничен.")
        return

    user_id = message.from_user.id
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        # 1. Скачиваем голосовое сообщение в память
        voice_file = await bot.get_file(message.voice.file_id)
        voice_buffer = io.BytesIO()
        await bot.download_file(voice_file.file_path, destination=voice_buffer)
        audio_bytes = voice_buffer.getvalue()

        # 2. Упаковываем аудио для Gemini (Telegram передает войсы в кодеке Opus/OGG)
        audio_part = Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")

        prompt = [
            audio_part,
            "Внимательно послушай это голосовое сообщение и ответь на него текстом по существу вопроса."
        ]

        # 3. Запрос к нейросети
        reply_text = await call_gemini_with_retry(prompt)
        await message.answer(reply_text, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка при обработке аудио: {e}")
        await message.answer("Не удалось обработать голосовое сообщение. Попробуйте еще раз.")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработка изображений."""
    if not is_user_allowed(message.from_user.id):
        await message.answer("Доступ ограничен.")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        photo = message.photo[-1]
        photo_file = await bot.get_file(photo.file_id)
        photo_buffer = io.BytesIO()
        await bot.download_file(photo_file.file_path, destination=photo_buffer)

        image_part = Part.from_bytes(data=photo_buffer.getvalue(), mime_type="image/jpeg")
        user_caption = message.caption or "Что изображено на этом фото? Опиши подробно."

        prompt = [image_part, user_caption]
        reply_text = await call_gemini_with_retry(prompt)
        await message.answer(reply_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        await message.answer("Не удалось проанализировать изображение.")

@dp.message(F.text)
async def handle_text(message: types.Message):
    """Обработка стандартных текстовых запросов с памятью контекста."""
    if not is_user_allowed(message.from_user.id):
        await message.answer("Доступ ограничен.")
        return

    user_id = message.from_user.id
    user_text = message.text

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    if user_id not in chat_histories:
        chat_histories[user_id] = []

    # Добавляем реплику пользователя
    chat_histories[user_id].append({"role": "user", "parts": [{"text": user_text}]})

    # Обрезаем историю при превышении лимита
    if len(chat_histories[user_id]) > MAX_HISTORY_MESSAGES:
        chat_histories[user_id] = chat_histories[user_id][-MAX_HISTORY_MESSAGES:]

    try:
        reply_text = await call_gemini_with_retry(chat_histories[user_id])
        # Сохраняем ответ модели в историю
        chat_histories[user_id].append({"role": "model", "parts": [{"text": reply_text}]})

        await message.answer(reply_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка при генерации текста: {e}")
        await message.answer("Произошла ошибка при обращении к нейросети. Попробуйте еще раз чуть позже.")

# ----------------- ВЕБ-СЕРВЕР И ВНУТРЕННИЙ ПИНГЕР -----------------

async def health_check_handler(request):
    """Эндпоинт для проверок Render и UptimeRobot."""
    return web.Response(text="Bot is running OK", status=200)

async def self_ping_task():
    """Фоновый пингер на случай, если UptimeRobot отключится."""
    await asyncio.sleep(60)  # Ждем минуту после старта
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if RENDER_EXTERNAL_URL:
                    async with session.get(RENDER_EXTERNAL_URL) as resp:
                        logger.info(f"Self-ping выполнен, статус: {resp.status}")
            except Exception as e:
                logger.warning(f"Ошибка self-ping: {e}")
            await asyncio.sleep(600)  # Пинг раз в 10 минут

async def start_web_server():
    """Запуск легковесного aiohttp сервера для прохождения Health Check на Render."""
    app = web.Application()
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/healthz", health_check_handler)

    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {port}")

# ----------------- ГЛАВНАЯ ТОЧКА ВХОДА -----------------

async def main():
    # Запускаем локальный веб-сервер для Render
    await start_web_server()

    # Запускаем фоновую задачу самопинга
    asyncio.create_task(self_ping_task())

    # Сбрасываем старые накопившиеся апдейты, чтобы не ловить спам при рестарте
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот готов к приему сообщений!")

    # Старт поллинга
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
