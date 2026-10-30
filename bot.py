import asyncio
import io
import os
import sqlite3
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from google import genai
from google.genai import types as genai_types

# ================= НАСТРОЙКИ =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

if not TELEGRAM_BOT_TOKEN:
    sys.exit("ОШИБКА: TELEGRAM_BOT_TOKEN не задан!")

if not GEMINI_API_KEY:
    sys.exit("ОШИБКА: GEMINI_API_KEY не задан!")

# Флагманская модель третьего поколения
GEMINI_MODEL = "gemini-3.5-pro"
DB_PATH = "chat_history.db"

SYSTEM_INSTRUCTION = """
Ты — Gemini, передовой интеллектуальный ассистент от Google.
Твой стиль общения:
- Умный, эрудированный, точный, объективный и вежливый.
- Форматируй ответы в чистый Markdown: выделяй главное жирным, структурируй списки, делай логичные отступы.
- Оформляй фрагменты кода с указанием языка (```python, ```cpp, ```bash).
- Отвечай подробно, полно и с высокой глубиной погружения в вопрос, избегая пустых заполнителей текста.
"""

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ================= РАБОТА С БАЗОЙ ДАННЫХ (SQLite) =================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_history_msg(user_id: int, role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    conn.commit()
    conn.close()


def load_user_history(user_id: int, limit: int = 14):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    
    rows.reverse()
    formatted_history = []
    for role, content in rows:
        formatted_history.append(
            genai_types.Content(
                role=role,
                parts=[genai_types.Part.from_text(text=content)]
            )
        )
    return formatted_history


def clear_user_history(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================

def split_text(text: str, max_length: int = 4000) -> list[str]:
    """Разбивает длинный ответ на части по абзацам для лимита Telegram."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while len(text) > max_length:
        split_idx = text.rfind("\n\n", 0, max_length)
        if split_idx == -1:
            split_idx = text.rfind("\n", 0, max_length)
        if split_idx == -1:
            split_idx = text.rfind(" ", 0, max_length)
        if split_idx == -1:
            split_idx = max_length

        chunks.append(text[:split_idx].strip())
        text = text[split_idx:].strip()

    if text:
        chunks.append(text)
    return chunks


async def send_response_safely(message: types.Message, full_text: str):
    chunks = split_text(full_text)
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode="Markdown")
        except Exception:
            await message.answer(chunk)


# ================= ОБРАБОТЧИКИ СООБЩЕНИЙ =================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "✨ **Gemini 3.5 Pro готов к работе.**\n\n"
        "Вы можете:\n"
        "• Задавать сложные технические вопросы и вести диалог в контексте\n"
        "• Отправлять фотографии, схемы и скриншоты ошибок для анализа\n"
        "• Скидывать файлы кода (`.py`, `.cpp`, `.log`, `.json`, `.txt`) на ревью\n\n"
        "Команды:\n"
        "• `/clear` — сбросить контекст диалога\n"
        "• `/model` — статус текущей модели",
        parse_mode="Markdown"
    )


@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    clear_user_history(message.from_user.id)
    await message.answer("🧹 Контекст диалога очищен. Начинаем новую тему!")


@dp.message(Command("model"))
async def cmd_model(message: types.Message):
    await message.answer(f"🧠 Текущая языковая модель: **{GEMINI_MODEL}**", parse_mode="Markdown")


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    # Скачивание фотографии
    photo_file = await bot.get_file(message.photo[-1].file_id)
    photo_stream = io.BytesIO()
    await bot.download_file(photo_file.file_path, destination=photo_stream)
    image_bytes = photo_stream.getvalue()

    user_caption = message.caption or "Детально проанализируй это изображение."
    history = load_user_history(user_id)

    chat = ai_client.chats.create(
        model=GEMINI_MODEL,
        history=history,
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.4
        )
    )

    try:
        content_payload = [
            genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            user_caption
        ]
        response = chat.send_message(content_payload)
        answer = response.text

        save_history_msg(user_id, "user", f"[Фото с описанием: {user_caption}]")
        save_history_msg(user_id, "model", answer)

        await send_response_safely(message, answer)
    except Exception as e:
        print(f"Ошибка Vision: {e}")
        await message.answer("⚠️ Не удалось проанализировать изображение. Попробуйте еще раз.")


@dp.message(F.document)
async def handle_document(message: types.Message):
    """Анализ текстовых документов и файлов кода"""
    doc = message.document
    valid_extensions = (".txt", ".py", ".cpp", ".h", ".json", ".log", ".csv", ".md", ".sh")

    if not any(doc.file_name.lower().endswith(ext) for ext in valid_extensions):
        await message.answer("Поддерживаются текстовые файлы: .txt, .py, .cpp, .json, .log, .csv, .md")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    doc_file = await bot.get_file(doc.file_id)
    stream = io.BytesIO()
    await bot.download_file(doc_file.file_path, destination=stream)
    file_content = stream.getvalue().decode("utf-8", errors="ignore")

    user_caption = message.caption or f"Проанализируй прикрепленный файл {doc.file_name}:"
    prompt = f"{user_caption}\n\n```{doc.file_name}\n{file_content}\n```"

    user_id = message.from_user.id
    history = load_user_history(user_id)

    chat = ai_client.chats.create(
        model=GEMINI_MODEL,
        history=history,
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.3
        )
    )

    try:
        response = chat.send_message(prompt)
        answer = response.text

        save_history_msg(user_id, "user", f"[Файл {doc.file_name}]")
        save_history_msg(user_id, "model", answer)

        await send_response_safely(message, answer)
    except Exception as e:
        print(f"Ошибка Document: {e}")
        await message.answer("⚠️ Ошибка при чтении файла.")


@dp.message(F.text)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    user_prompt = message.text

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    history = load_user_history(user_id)

    chat = ai_client.chats.create(
        model=GEMINI_MODEL,
        history=history,
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.6
        )
    )

    try:
        response = chat.send_message(user_prompt)
        answer = response.text

        save_history_msg(user_id, "user", user_prompt)
        save_history_msg(user_id, "model", answer)

        await send_response_safely(message, answer)
    except Exception as e:
        print(f"Ошибка Gemini: {e}")
        await message.answer("⚠️ Не удалось получить ответ от модели. Повторите запрос позже.")


# ================= ВЕБ-СЕРВЕР И ЗАПУСК =================

async def web_health_check(request):
    return web.Response(text="Gemini 3.5 Web Assistant is active!")


async def main():
    init_db()

    app = web.Application()
    app.router.add_get("/", web_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health-check сервер запущен на порту {PORT}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
