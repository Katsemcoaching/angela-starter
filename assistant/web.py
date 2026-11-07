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


@web_app.get("/oura/auth")
async def oura_auth_start():
    """Открой эту ссылку в браузере, чтобы разрешить доступ к кольцу Oura."""
    if not config.ENABLE_OURA:
        return HTMLResponse("<h2>Модуль кольца выключен</h2>", status_code=400)
    if not config.OURA_CLIENT_ID:
        return HTMLResponse("<h2>OURA_CLIENT_ID не задан</h2>", status_code=500)
    from assistant.oura_auth import get_auth_url
    return HTMLResponse(
        '<h2>Авторизация кольца Oura</h2>'
        '<p>Дальше войди тем аккаунтом Oura, на котором кольцо.</p>'
        f'<p><a href="{get_auth_url()}">Нажми, чтобы разрешить доступ</a></p>'
    )


@web_app.get("/oura/status")
async def oura_status():
    """Диагностика цепочки кольца: токен → Oura → база.

    Нужна потому, что изнутри Railway и Supabase агенту не видны. Отдаёт
    только состояние звеньев: есть/нет, коды ответов, даты. Ни ключей, ни
    самих показателей здоровья здесь нет — страница открыта наружу.
    """
    import asyncio
    from datetime import date, timedelta

    import httpx

    out: dict = {"enabled": config.ENABLE_OURA}
    if not config.ENABLE_OURA:
        return out
    try:
        from assistant.db import supabase
        from assistant.oura_auth import KEY, get_access_token

        rows = (supabase.table("oauth_tokens").select("expires_at")
                .eq("key", KEY).limit(1).execute().data)
        out["token_saved"] = bool(rows)
        out["token_expires_at"] = rows[0].get("expires_at") if rows else None

        token = await asyncio.to_thread(get_access_token)
        out["token_usable"] = bool(token)

        if token:
            end = date.today()
            start = end - timedelta(days=3)
            resp = await asyncio.to_thread(
                lambda: httpx.get(
                    "https://api.ouraring.com/v2/usercollection/daily_sleep",
                    params={"start_date": start.isoformat(), "end_date": end.isoformat()},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20,
                )
            )
            out["oura_http"] = resp.status_code
            try:
                out["oura_rows"] = len(resp.json().get("data", []))
            except Exception:
                out["oura_body"] = resp.text[:200]

        # Разведка новых разделов: доступны ли и как называются поля.
        # Возвращаем ТОЛЬКО имена полей — теги это личные заметки Кати.
        if token:
            probe: dict = {}
            end = date.today()
            start = end - timedelta(days=14)
            for name in ("daily_resilience", "enhanced_tag"):
                try:
                    r = await asyncio.to_thread(
                        lambda n=name: httpx.get(
                            f"https://api.ouraring.com/v2/usercollection/{n}",
                            params={"start_date": start.isoformat(),
                                    "end_date": end.isoformat()},
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=20,
                        )
                    )
                    info: dict = {"http": r.status_code}
                    if r.status_code == 200:
                        rows = r.json().get("data", [])
                        info["rows"] = len(rows)
                        if rows:
                            info["fields"] = sorted(rows[0].keys())
                    else:
                        info["body"] = r.text[:160]
                    probe[name] = info
                except Exception as exc:
                    probe[name] = {"error": f"{type(exc).__name__}: {exc}"}
            out["probe"] = probe

        last = (supabase.table("oura_daily").select("*")
                .order("day", desc=True).limit(5).execute().data)
        out["db_total"] = len(supabase.table("oura_daily").select("day")
                              .limit(200).execute().data)
        # Какие поля реально заполнены — только «да/нет», без значений.
        watch = ("sleep_score", "readiness_score", "temp_deviation", "rhr",
                 "hrv", "total_min", "deep_min", "awake_min", "efficiency",
                 "restless", "stress_high_min", "recovery_high_min",
                 "stress_summary")
        out["db_last"] = [
            {"day": r["day"],
             "filled": [f for f in watch if r.get(f) is not None]}
            for r in last
        ]
        out["db_resilience_days"] = len(
            [r for r in last if r.get("resilience") is not None])
        # Метки — своя таблица. Считаем только количество и последний день:
        # содержимое меток это личные заметки, наружу их не отдаём.
        try:
            tags = (supabase.table("oura_tags").select("day")
                    .order("day", desc=True).limit(200).execute().data)
            out["db_tags"] = len(tags)
            out["db_tags_last_day"] = tags[0]["day"] if tags else None
        except Exception as exc:
            out["db_tags_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


@web_app.get("/oura/callback")
async def oura_callback(code: str = "", error: str = ""):
    """Сюда Oura возвращает после согласия — меняем код на токены."""
    if error:
        return HTMLResponse(f"<h2>Ошибка: {error}</h2>", status_code=400)
    if not code:
        return HTMLResponse("<h2>Нет кода авторизации</h2>", status_code=400)
    try:
        from assistant.oura_auth import exchange_code
        exchange_code(code)
        # Сразу тянем историю: сбор стоит на старте бота и на утро, а
        # авторизация проходит позже. Без этого данные появились бы только
        # на следующий день.
        import asyncio

        from assistant.tools.oura import collect
        days = await asyncio.to_thread(collect, 60)
        return HTMLResponse(
            f"<h2>Готово ✅</h2><p>Кольцо подключено. Забрано дней: {days}.</p>"
            "<p>Можно закрыть вкладку и вернуться в бота.</p>"
        )
    except Exception as exc:
        logger.exception("ошибка callback Oura")
        return HTMLResponse(f"<h2>Ошибка: {exc}</h2>", status_code=500)


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
