"""ОПЦИОНАЛЬНЫЙ модуль: Google Calendar (события на сегодня/ближайшие + создание).

Выключен по умолчанию. Включить: ENABLE_GCAL=true в .env + ключи Google.
Сначала пройди общий шаг авторизации Google. Подробно — docs/05-add-calendar.md
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from assistant.config import TIMEZONE
from assistant.google_auth import get_access_token

logger = logging.getLogger(__name__)
API = "https://www.googleapis.com/calendar/v3"

PROMPT_ADDON = """\
МОДУЛЬ КАЛЕНДАРЯ включён. в утреннем чекине показывай встречи на сегодня (время + название).
календарей может быть несколько (личный, рабочий) — у каждой встречи есть поле calendar.
если встречи из разных календарей, помечай откуда какая. если из одного — не уточняй, это шум.
если просят «поставь встречу / добавь в календарь / создай зум» — вызови gcal_create_event
(время в ISO). если сказали, в какой календарь («в рабочий») — передай его в calendar.
иначе календарь сам не дёргай.
"""

TOOLS = [
    {
        "name": "gcal_today",
        "description": "События календаря на сегодня (время + название).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "gcal_upcoming",
        "description": "Ближайшие события (по умолчанию на 24 часа вперёд).",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "default": 24}},
        },
    },
    {
        "name": "gcal_create_event",
        "description": "Создать событие. Время в ISO: 2026-01-31T09:00:00. attendees — email-ы участников.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO начало"},
                "end_time": {"type": "string", "description": "ISO конец"},
                "description": {"type": "string"},
                "location": {"type": "string", "description": "Место или ссылка (напр. на Zoom)"},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "calendar": {
                    "type": "string",
                    "description": "Название календаря, если попросили конкретный (напр. «рабочий»). "
                                   "Без него событие идёт в основной.",
                },
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
]

# Служебные календари Google, которые есть у всех и только зашумляют утро:
# праздники страны, дни рождения из контактов, номера недель.
_NOISE = ("holiday@group.v.calendar.google.com",
          "#contacts@group.v.calendar.google.com",
          "#weeknum@group.v.calendar.google.com")


def _calendars(token: str) -> list[dict]:
    """Все календари, которые видит аккаунт: личный, рабочий, расшаренные.

    Если перечень не отдался (нет права, сеть) — молча работаем по основному,
    как раньше. Лучше показать одни встречи, чем упасть.
    """
    try:
        resp = httpx.get(f"{API}/users/me/calendarList", headers={
            "Authorization": f"Bearer {token}",
        }, timeout=15)
        resp.raise_for_status()
        found = []
        for c in resp.json().get("items", []):
            cid = c.get("id", "")
            if c.get("deleted") or c.get("hidden") or cid.endswith(_NOISE):
                continue
            found.append({"id": cid, "name": c.get("summary", cid)})
        return found or [{"id": "primary", "name": ""}]
    except Exception:
        logger.exception("не вышло получить список календарей — работаю по основному")
        return [{"id": "primary", "name": ""}]


def _events_between(start: datetime, end: datetime) -> list[dict] | dict:
    token = get_access_token()
    if not token:
        return {"error": "Google не авторизован — открой /google/auth у бота"}

    events = []
    calendars = _calendars(token)
    failed = 0
    for cal in calendars:
        try:
            resp = httpx.get(f"{API}/calendars/{quote(cal['id'], safe='')}/events", headers={
                "Authorization": f"Bearer {token}",
            }, params={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
            }, timeout=15)
            resp.raise_for_status()
        except Exception:
            # Один сломанный календарь не должен уносить остальные.
            logger.exception("ошибка Calendar (%s)", cal["name"] or cal["id"])
            failed += 1
            continue
        for e in resp.json().get("items", []):
            events.append({
                "title": e.get("summary", "(без названия)"),
                "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""),
                "location": e.get("location", ""),
                "calendar": cal["name"],
            })

    if failed == len(calendars):
        # Ни один календарь не ответил. Пустой список тут читался бы как
        # «встреч нет» — а это не так, мы просто не смогли посмотреть.
        return {"error": "не удалось прочитать календарь — посмотри логи"}

    events.sort(key=lambda e: e["start"])
    return events


def _gcal_today(data: dict):
    now = datetime.now(TIMEZONE)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return _events_between(start, start + timedelta(days=1))


def _gcal_upcoming(data: dict):
    now = datetime.now(TIMEZONE)
    return _events_between(now, now + timedelta(hours=data.get("hours", 24)))


def _gcal_create_event(data: dict) -> dict:
    token = get_access_token()
    if not token:
        return {"error": "Google не авторизован — открой /google/auth у бота"}
    body = {
        "summary": data["title"],
        "start": {"dateTime": data["start_time"], "timeZone": str(TIMEZONE)},
        "end": {"dateTime": data["end_time"], "timeZone": str(TIMEZONE)},
    }
    if data.get("description"):
        body["description"] = data["description"]
    if data.get("location"):
        body["location"] = data["location"]
    if data.get("attendees"):
        body["attendees"] = [{"email": e} for e in data["attendees"]]

    # По умолчанию — основной календарь. Если попросили конкретный, ищем его
    # по названию; не нашли — не угадываем, кладём в основной и говорим об этом.
    target, note = "primary", ""
    asked = (data.get("calendar") or "").strip().lower()
    if asked:
        match = next((c for c in _calendars(token) if asked in c["name"].lower()), None)
        if match:
            target = match["id"]
        else:
            note = f"календарь «{data['calendar']}» не нашёлся, положила в основной"

    try:
        resp = httpx.post(f"{API}/calendars/{quote(target, safe='')}/events", headers={
            "Authorization": f"Bearer {token}",
        }, params={"sendUpdates": "all"}, json=body, timeout=15)
        resp.raise_for_status()
        e = resp.json()
        result = {"created": True, "title": e.get("summary", ""), "link": e.get("htmlLink", "")}
        if note:
            result["note"] = note
        return result
    except Exception as exc:
        logger.exception("ошибка создания события")
        return {"error": str(exc)}


HANDLERS = {
    "gcal_today": _gcal_today,
    "gcal_upcoming": _gcal_upcoming,
    "gcal_create_event": _gcal_create_event,
}
