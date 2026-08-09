"""Маленький веб-сервер: health-check для Railway + авторизация Google.

Страницы /google/* нужны только если включены модули почты или календаря
(ENABLE_GMAIL / ENABLE_GCAL). Для базовой версии достаточно того, что
сервер просто отвечает «ok» — Railway по этому понимает, что бот жив.
"""

import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from assistant import config

logger = logging.getLogger(__name__)

web_app = FastAPI(title="assistant bot")


@web_app.get("/")
async def health():
    return {"status": "ok"}


@web_app.get("/google/auth")
async def google_auth_start(account: str = "work"):
    """Открой эту ссылку в браузере, чтобы разрешить доступ к Google.

    ?account=personal — подключить второй (личный) ящик. Без параметра —
    рабочий, он же по умолчанию и на нём висит календарь.
    """
    if not (config.ENABLE_GMAIL or config.ENABLE_GCAL):
        return HTMLResponse("<h2>Модули Google выключены</h2>", status_code=400)
    if not config.GOOGLE_CLIENT_ID:
        return HTMLResponse("<h2>GOOGLE_CLIENT_ID не задан</h2>", status_code=500)
    from assistant.google_auth import PERSONAL, WORK, get_auth_url
    key = PERSONAL if account == "personal" else WORK
    подпись = "ЛИЧНЫЙ" if key == PERSONAL else "РАБОЧИЙ"
    return HTMLResponse(
        f'<h2>Авторизация Google — {подпись} ящик</h2>'
        f'<p>Дальше выбери именно <b>{подпись.lower()}</b> аккаунт.</p>'
        f'<p><a href="{get_auth_url(key)}">Нажми, чтобы разрешить доступ</a></p>'
    )


@web_app.get("/google/callback")
async def google_callback(code: str = "", error: str = "", state: str = ""):
    """Сюда Google возвращает после согласия — меняем код на токены."""
    if error:
        return HTMLResponse(f"<h2>Ошибка: {error}</h2>", status_code=400)
    if not code:
        return HTMLResponse("<h2>Нет кода авторизации</h2>", status_code=400)
    try:
        from assistant.google_auth import ACCOUNTS, PERSONAL, WORK, exchange_code
        # state вернулся от Google тем же, каким мы его отправили
        account = state if state in ACCOUNTS else WORK
        exchange_code(code, account)
        подпись = "личный" if account == PERSONAL else "рабочий"
        return HTMLResponse(
            f"<h2>Готово ✅</h2><p>Подключён <b>{подпись}</b> ящик. "
            f"Можно закрыть вкладку и вернуться в бота.</p>"
        )
    except Exception as exc:
        logger.exception("ошибка callback Google")
        return HTMLResponse(f"<h2>Ошибка: {exc}</h2>", status_code=500)
