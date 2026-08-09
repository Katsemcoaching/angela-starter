"""Точка входа: `python -m assistant`.

Запускает разом: веб-сервер (health + Google OAuth), Telegram-бота
и планировщик чекинов. Перед стартом проверяет, что заполнены
обязательные переменные окружения, и понятно ругается, если нет.
"""

import asyncio
import logging
import threading

import uvicorn

from assistant import config
from assistant.bot import create_application
from assistant.scheduler import create_scheduler, run_catchup, set_sender
from assistant.web import web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
# httpx пишет в лог полный адрес каждого запроса, а токен бота стоит прямо
# в адресе телеграма — то есть светится в логах Railway открытым текстом.
# Оставляем от него только предупреждения и ошибки.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _start_web() -> None:
    uvicorn.run(web_app, host="0.0.0.0", port=config.PORT, log_level="warning")


async def run() -> None:
    missing = config.missing_required()
    if missing:
        logger.error("Не заданы обязательные переменные: %s", ", ".join(missing))
        logger.error("Заполни их в .env (локально) или в Railway → Variables. См. docs/01-setup.md")
        return

    # Веб-сервер в фоне — нужен Railway для health-check и Google OAuth.
    threading.Thread(target=_start_web, daemon=True).start()
    logger.info("веб-сервер на порту %s", config.PORT)

    # Telegram-бот.
    app = create_application()

    # Планировщик шлёт чекины в твой чат через этого бота.
    async def send(text: str) -> None:
        await app.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=text)

    set_sender(send)

    # Сначала поднимаем бота, потом планировщик — чтобы плановые задачи
    # не сработали раньше, чем app.bot готов отправлять сообщения.
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("бот запущен, слушаю сообщения…")

    scheduler = create_scheduler()
    scheduler.start()
    logger.info("планировщик запущен (TZ=%s)", config.TIMEZONE)

    await run_catchup()  # дослать пропущенный чекин, если время уже прошло

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
