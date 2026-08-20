"""Конфигурация — всё берётся из переменных окружения (env).

Локально: скопируй .env.example → .env и заполни.
На Railway: задай те же переменные во вкладке Variables.

Ничего секретного не хранится в коде. Файл можно не трогать —
всё настраивается через .env. См. docs/01-setup.md.
"""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "false") -> bool:
    """Прочитать булеву переменную окружения (true/1/yes/on → True)."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── ОБЯЗАТЕЛЬНО: три вещи, без которых бот не запустится ──────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: int = int(os.getenv("TELEGRAM_CHAT_ID") or 0)
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

# ── Модели Claude ────────────────────────────────────────────────────
# Чекины (утро/вечер/неделя) идут на «умной» модели — там важен нюанс.
# Обычный чат — на быстрой и дешёвой. Менять не обязательно.
# Разговор шёл на Haiku — самой маленькой модели, и именно там нужна тонкость.
# 20 августа перевели на Sonnet: главный рычаг «глубины», важнее любого промпта.
# Примерно втрое дороже, ~$40/мес вместо ~$13 при тридцати сообщениях в день.
MODEL_CHAT: str = os.getenv("MODEL_CHAT", "claude-sonnet-5")
MODEL_CHECKIN: str = os.getenv("MODEL_CHECKIN", "claude-sonnet-4-6")
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))
MAX_TOOL_ROUNDS: int = int(os.getenv("MAX_TOOL_ROUNDS", "4"))
# Сколько последних сообщений подтягивать в разговор автоматически.
# Всё, что старше, лежит в базе и достаётся инструментом search_history.
HISTORY_LIMIT: int = int(os.getenv("HISTORY_LIMIT", "30"))

# ── Расписание (часы в твоём часовом поясе) ──────────────────────────
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
# Часы под Катю: утро пораньше, вечер попозже. Менять можно и здесь,
# и переменными в Railway — переменная перебивает это значение.
MORNING_HOUR: int = int(os.getenv("MORNING_HOUR", "7"))
EVENING_HOUR: int = int(os.getenv("EVENING_HOUR", "22"))
WEEKLY_REVIEW_HOUR: int = int(os.getenv("WEEKLY_REVIEW_HOUR", "11"))  # воскресенье

# ── Опциональные модули (по умолчанию ВЫКЛ) ──────────────────────────
# Каждый включается в .env и имеет свой урок в docs/.
ENABLE_CYCLE: bool = _flag("ENABLE_CYCLE")              # docs/03-add-cycle.md
ENABLE_GMAIL: bool = _flag("ENABLE_GMAIL")              # docs/04-add-gmail.md
ENABLE_GCAL: bool = _flag("ENABLE_GCAL")                # docs/05-add-calendar.md
ENABLE_REMINDERS: bool = _flag("ENABLE_REMINDERS")     # docs/06-add-reminders.md
ENABLE_WEEKLY_REVIEW: bool = _flag("ENABLE_WEEKLY_REVIEW")  # docs/06-add-reminders.md

# ── Ключи для опциональных интеграций ────────────────────────────────
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")      # голосовые (Whisper)
GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")  # Gmail + Calendar
GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
# Куда Google вернёт пользователя после авторизации.
# Пример: https://your-app.up.railway.app/google/callback
GOOGLE_REDIRECT_URI: str = os.getenv("GOOGLE_REDIRECT_URI", "")

# ── Сервер (Railway сам задаёт PORT) ─────────────────────────────────
PORT: int = int(os.getenv("PORT", "8080"))


def missing_required() -> list[str]:
    """Вернуть список незаполненных обязательных переменных (для понятной ошибки)."""
    required = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
    }
    return [name for name, value in required.items() if not value]
