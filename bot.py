import os
import io
import re
import asyncio
import logging
import urllib.parse
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
# Например: ALLOWED_USERS = [123456789, 987654321]
# Если оставить список пустым [], доступ будет открыт для всех
ALLOWED_USERS = []

# Системный промпт модели с правилами для математики
SYSTEM_INSTRUCTION = (
    "Ты — универсальный, умный и дружелюбный персональный ассистент.\n"
    "ПРАВИЛО ДЛЯ МАТЕМАТИКИ И ФОРМУЛ:\n"
    "Если в ответе присутствуют сложные математические выражения, корни, дроби, "
    "интегралы, пределы или матрицы, обязательно оборачивай их в теги LaTeX блочно через двоичные доллары: $$...$$\n"
    "Например:\n"
    "$$x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$$\n"
    "Текстовое пояснение пиши обычным текстом вне тегов."
)

# Инициализация клиентов
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Локальная история диалогов в оперативной памяти: {user_id: [список сообщений]}
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

def extract_and_render_formulas(text: str):
    """
    Находит все блоки $$...$$ в ответе модели и генерирует ссылки
    на чистые PNG-изображения формул без лишних служебных надписей.
    """
    pattern = r"\$\$(.*?)\$\$"
    matches = re.findall(pattern, text, flags=re.DOTALL)
    
    image_urls = []
    for formula in matches:
        clean_formula = formula.strip()
        if not clean_formula:
            continue
        
        # Передаем размер \dpi{200} прямо в теле формулы, 
        # а белый фон и параметры рендера задаем через параметры URL сервиса
        latex_payload = f"\\dpi{{200}} {clean_formula}"
        encoded_query = urllib.parse.quote(latex_payload)
        
        # bg_white задается параметром запроса через &bg=white или в формате SVG/PNG
        url = f"https://latex.codecogs.com/png.image?{encoded_query}"
        image_urls.append((clean_formula, url))
        
    return image_urls

async def call_gemini_with_retry(contents, retries=3, delay=3):
    """Вызов модели gemini-3.5-flash-lite с автоматическим веб-поиском."""
    for attempt in range(retries):
        try:
            response = ai_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.7,
                    # ПОДКЛЮЧАЕМ ПОИСК GOOGLE
                    tools=[{"google_search": {}}]
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
        await message.answer("Извините, доступ к этому ассистенту ограничен.")
        return

    chat_histories[message.from_user.id] = []
    welcome_text = (
        "Привет! Я персональный ИИ-ассистент на базе Gemini.\n\n"
        "Чем я могу помочь:\n"
        "• Отвечать на вопросы, решать задачи и красиво рендерить формулы\n"
        "• Принимать и слушать голосовые сообщения\n"
        "• Анализировать изображения\n\n"
        "Для сброса диалога нажмите кнопку «🧹 Очистить контекст»."
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
        # Скачиваем голосовое сообщение в буфер памяти
        voice_file = await bot.get_file(message.voice.file_id)
        voice_buffer = io.BytesIO()
        await bot.download_file(voice_file.file_path, destination=voice_buffer)
        audio_bytes = voice_buffer.getvalue()

        # Упаковываем аудио для Gemini (MIME-тип audio/ogg)
        audio_part = Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
        prompt = [
            audio_part,
            "Внимательно послушай это голосовое сообщение и ответь на него текстом по существу вопроса."
        ]

        reply_text = await call_gemini_with_retry(prompt)
        
        # Проверяем наличие формул в ответе
        formulas = extract_and_render_formulas(reply_text)
        await message.answer(reply_text)

        # Если модель сгенерировала формулы — присылаем их картинками
        for raw_formula, img_url in formulas:
            try:
                await message.answer_photo(photo=img_url, caption=f"Формула: {raw_formula}")
            except Exception as img_err:
                logger.warning(f"Ошибка отправки картинки формулы: {img_err}")

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

        formulas = extract_and_render_formulas(reply_text)
        await message.answer(reply_text)

        for raw_formula, img_url in formulas:
            try:
                await message.answer_photo(photo=img_url, caption=f"Формула: {raw_formula}")
            except Exception as img_err:
                logger.warning(f"Ошибка отправки картинки формулы: {img_err}")

    except Exception as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        await message.answer("Не удалось проанализировать изображение.")

@dp.message(F.text)
async def handle_text(message: types.Message):
    """Обработка текста с памятью контекста и рендерингом формул."""
    if not is_user_allowed(message.from_user.id):
        await message.answer("Доступ ограничен.")
        return

    user_id = message.from_user.id
    user_text = message.text

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    if user_id not in chat_histories:
        chat_histories[user_id] = []

    chat_histories[user_id].append({"role": "user", "parts": [{"text": user_text}]})

    # Ограничение длины контекста
    if len(chat_histories[user_id]) > MAX_HISTORY_MESSAGES:
        chat_histories[user_id] = chat_histories[user_id][-MAX_HISTORY_MESSAGES:]

    try:
        reply_text = await call_gemini_with_retry(chat_histories[user_id])
        chat_histories[user_id].append({"role": "model", "parts": [{"text": reply_text}]})

        # Проверяем, есть ли математические выражения в ответе
        formulas = extract_and_render_formulas(reply_text)

        # Отправляем основной текст
        await message.answer(reply_text)

        # Отправляем найденные формулы отдельными графическими карточками
        for raw_formula, img_url in formulas:
            try:
                await message.answer_photo(photo=img_url, caption=f"Формула: {raw_formula}")
            except Exception as img_err:
                logger.warning(f"Ошибка отправки картинки формулы: {img_err}")

    except Exception as e:
        logger.error(f"Ошибка при генерации текста: {e}")
        await message.answer("Произошла ошибка при обращении к нейросети. Попробуйте еще раз чуть позже.")

# ----------------- ВЕБ-СЕРВЕР И ВНУТРЕННИЙ ПИНГЕР -----------------

async def health_check_handler(request):
    """Эндпоинт для UptimeRobot и внутреннего пингера."""
    return web.Response(text="Bot is running OK", status=200)

async def self_ping_task():
    """Фоновый пинг каждые 10 минут."""
    await asyncio.sleep(60)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if RENDER_EXTERNAL_URL:
                    async with session.get(RENDER_EXTERNAL_URL) as resp:
                        logger.info(f"Self-ping выполнен, статус: {resp.status}")
            except Exception as e:
                logger.warning(f"Ошибка self-ping: {e}")
            await asyncio.sleep(600)

async def start_web_server():
    """Запуск сервера aiohttp на порту Render."""
    app = web.Application()
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/healthz", health_check_handler)

    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {port}")

# ----------------- СТАРТ ПРИЛОЖЕНИЯ -----------------

async def main():
    await start_web_server()
    asyncio.create_task(self_ping_task())

    # Сбрасываем старые накопившиеся апдейты
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен на модели gemini-3.5-flash-lite!")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
