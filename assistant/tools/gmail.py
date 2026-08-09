"""ОПЦИОНАЛЬНЫЙ модуль: чтение Gmail (только чтение, ничего не отправляет).

Выключен по умолчанию. Включить: ENABLE_GMAIL=true в .env + ключи Google.
Сначала пройди общий шаг авторизации Google. Подробно — docs/04-add-gmail.md
"""

import logging

import httpx

from assistant.google_auth import PERSONAL, authorized_accounts, get_access_token

logger = logging.getLogger(__name__)
API = "https://www.googleapis.com/gmail/v1"

# Как называть ящики в ответах — по-человечески, а не ключами из базы.
_NAMES = {PERSONAL: "личная почта"}
_WORK_NAME = "рабочая почта"

PROMPT_ADDON = """\
МОДУЛЬ ПОЧТЫ включён (только чтение). в утреннем чекине, если уместно, можешь одной строкой
упомянуть важные непрочитанные письма. отдельно показывай почту, только когда просят
(«что на почте», «есть важные письма»). не дёргай почту в каждом сообщении.
ящиков может быть два — у каждого письма есть поле «ящик». если письма из обоих,
помечай откуда какое. если из одного — не уточняй, это шум.
"""

TOOLS = [
    {
        "name": "gmail_unread",
        "description": "Непрочитанные письма из Gmail (от кого + тема). Покажи кратко.",
        "input_schema": {
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "gmail_important",
        "description": "Только важные/помеченные непрочитанные письма.",
        "input_schema": {
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 5}},
        },
    },
]


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _list_one(account: str, query: str, max_results: int) -> list[dict]:
    """Письма из одного ящика. Ошибку не поднимаем — вернём пометку."""
    token = get_access_token(account)
    if not token:
        return []
    name = _NAMES.get(account, _WORK_NAME)
    auth = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(f"{API}/users/me/messages",
                         headers=auth, params={"q": query, "maxResults": max_results}, timeout=15)
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
        out = []
        for m in messages:
            d = httpx.get(f"{API}/users/me/messages/{m['id']}", headers=auth, params={
                "format": "metadata", "metadataHeaders": ["From", "Subject"],
            }, timeout=15).json()
            headers = d.get("payload", {}).get("headers", [])
            out.append({
                "ящик": name,
                "from": _header(headers, "From"),
                "subject": _header(headers, "Subject"),
                "snippet": d.get("snippet", ""),
            })
        return out
    except Exception as exc:
        logger.exception("ошибка Gmail (%s)", name)
        return [{"ящик": name, "error": str(exc)}]


def _list(query: str, max_results: int) -> list[dict]:
    """Письма из всех подключённых ящиков. Один сломался — остальные покажем."""
    accounts = authorized_accounts()
    if not accounts:
        return [{"error": "Google не авторизован — открой /google/auth у бота"}]

    out = []
    for account in accounts:
        out.extend(_list_one(account, query, max_results))

    if not out:
        # Токены есть, но писем нет и ошибок не было — почта просто пустая.
        return []
    return out


def _gmail_unread(data: dict) -> list[dict]:
    return _list("is:unread", data.get("max_results", 10))


def _gmail_important(data: dict) -> list[dict]:
    return _list("is:unread (is:important OR is:starred)", data.get("max_results", 5))


HANDLERS = {"gmail_unread": _gmail_unread, "gmail_important": _gmail_important}
