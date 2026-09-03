"""Telegram-бот — принимает сообщения владельца и отвечает через Claude.

Бот личный: отвечает ТОЛЬКО на чат с твоим TELEGRAM_CHAT_ID, остальных
игнорирует. Память диалога хранится в Supabase и переживает рестарты.
"""

import logging
import tempfile

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from assistant import db, formatting
from assistant.agent import ask
from assistant.config import HISTORY_LIMIT, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from assistant.prompts import ASSISTANT_NAME

logger = logging.getLogger(__name__)

# Голосовые — опционально (нужен OPENAI_API_KEY). Без ключа бот попросит писать текстом.
_openai = None
if OPENAI_API_KEY:
    from openai import AsyncOpenAI
    _openai = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Быстрые кнопки — просто отправляют фразу, которую ассистент понимает.
KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("🔮 Фокус на сегодня"), KeyboardButton("🌙 Итоги дня")]],
    resize_keyboard=True,
    is_persistent=True,
)


def _is_owner(update: Update) -> bool:
    return bool(update.effective_chat) and update.effective_chat.id == TELEGRAM_CHAT_ID


async def _reply(update: Update, text: str) -> None:
    """Отправить ответ: жирный там, где модель поставила **звёздочки**.

    Если Telegram почему-то не принял разметку — шлём тот же кусок обычным
    текстом. Лучше сообщение без жирного, чем никакого сообщения.
    """
    for chunk in formatting.split_chunks(text):
        try:
            await update.message.reply_text(
                formatting.to_html(chunk),
                parse_mode=ParseMode.HTML,
                reply_markup=KEYBOARD,
            )
        except BadRequest:
            logger.exception("Telegram не принял разметку — шлю обычным текстом")
            await update.message.reply_text(chunk, reply_markup=KEYBOARD)


async def _process(update: Update, user_text: str, prefix: str = "") -> None:
    """Общий путь для текста и голоса: память → Claude → ответ → память."""
    await update.effective_chat.send_action("typing")
    history = db.get_recent_memory(limit=HISTORY_LIMIT)
    # пометка [голосовое] уходит и в Claude, не только в память: без неё
    # Анджелина не знает, что Катя диктовала, и правило про чистовик не
    # срабатывает в момент ответа (разбор 21 августа 2026)
    answer = await ask(f"{prefix}{user_text}", history=history)
    db.save_message("human", f"{prefix}{user_text}")
    db.save_message("ai", answer)
    await _reply(update, answer)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await update.message.reply_text(
        f"привет, я {ASSISTANT_NAME} 🌿\n\n"
        "буду рядом каждый день: утром спрошу про фокус и топ-3 задачи, "
        "вечером — что удалось. можешь просто писать мне в любой момент.\n\n"
        "/help — что я умею",
        reply_markup=KEYBOARD,
    )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await update.message.reply_text(
        "что я умею:\n"
        "• утренний чекин — фокус + топ-3 задачи на день\n"
        "• вечерний чекин — что удалось, итог дня\n"
        "• просто поговорить — я помню последние сообщения\n\n"
        "хочешь больше (почта, календарь, цикл, напоминания) — это включается "
        "по урокам в папке docs/.",
        reply_markup=KEYBOARD,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        logger.warning("сообщение из чужого чата — игнор")
        return
    if not update.message.text:
        return
    try:
        await _process(update, update.message.text)
    except Exception:
        logger.exception("ошибка обработки сообщения")
        await update.message.reply_text("ой, что-то пошло не так. попробуй ещё раз 🙏")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if not _openai:
        await update.message.reply_text("голосовые пока не подключены — напиши текстом 🙏")
        return
    try:
        voice = update.message.voice or update.message.audio
        voice_file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg") as tmp:
            await voice_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as audio:
                transcript = await _openai.audio.transcriptions.create(
                    model="whisper-1", file=audio,
                )
        text = (transcript.text or "").strip()
        if not text:
            await update.message.reply_text("не расслышала, повтори 🎤")
            return
        await _process(update, text, prefix="[голосовое] ")
    except Exception:
        logger.exception("ошибка обработки голосового")
        await update.message.reply_text("не получилось разобрать голосовое, попробуй ещё раз 🙏")


def create_application() -> Application:
    """Собрать Telegram Application со всеми хендлерами."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    return app
