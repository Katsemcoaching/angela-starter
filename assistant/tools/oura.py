"""ОПЦИОНАЛЬНЫЙ модуль: кольцо Oura — сон, пульс, HRV, стресс.

Выключен по умолчанию. Включить: ENABLE_OURA=true в .env.
Нужна таблица oura_daily (есть в schema.sql) и авторизация через
/oura/auth (см. assistant/oura_auth.py).

Зачем это Кате — одной строкой: у неё цикл перегруза (три дня на двухстах
процентах, на четвёртый обвал). Пульс покоя и HRV уходят от нормы раньше,
чем она сама это чувствует. Кольцо здесь — датчик раннего предупреждения,
а не коллекция красивых скоров.

⚠️ Главное ограничение: скоры Oura сравнивают человека С САМИМ СОБОЙ.
Пока накопленных дней мало, «отклонение» считать не от чего. Поэтому
инструменты честно возвращают days_of_data, а промпт запрещает делать
выводы, пока дней меньше CALIBRATION_DAYS.
"""

import logging
from datetime import date, timedelta

import httpx

from assistant.db import supabase
from assistant.oura_auth import get_access_token

logger = logging.getLogger(__name__)

API = "https://api.ouraring.com/v2/usercollection"

# Сколько дней нужно накопить, прежде чем говорить про «отклонение от нормы».
# Четырнадцать — не выдумка: столько же Oura просит на свой Resilience.
CALIBRATION_DAYS = 14


PROMPT_ADDON = f"""\
МОДУЛЬ КОЛЬЦА ORA включён. Ты видишь данные Катиного кольца: сон, пульс
покоя, HRV, температуру, стресс.

Кольцо — это НЕ новая роль. Ты остаёшься коучем и терапевтом. Данные
кольца — такой же факт о её теле, как день цикла: пользуешься молча, а не
превращаешься в фитнес-трекер.

Как пользоваться:
- цифры сами по себе Кате не нужны. Не сыпь показателями и не зачитывай
  сводки. Из данных делай ОДИН вывод про сегодня.
- говори словами, а не метриками: не «HRV 42 при базовой 55», а «тело
  третий день не восстанавливается».
- в ответе любого инструмента есть calibrating и days_of_data. Пока
  calibrating=true — нормы Кати ещё нет, кольцо её набирает.
  Тогда МОЖНО назвать факт («спала 7 часов 12 минут», «глубокой — сорок
  минут»), но НЕЛЬЗЯ оценивать. «Хорошие цифры», «неплохо», «выше нормы»,
  «тело в форме» — это всё оценки, а сравнивать пока не с чем. Скажи
  прямо: кольцо ещё набирает её норму, выводы будут позже.
  Про отклонения, разгон и звоночки в этот период — молчи.
- молчание по умолчанию. Сама тему кольца не поднимай, пока данные не
  говорят о чём-то важном или Катя не спросила.
- диагнозов не ставь. Плохой сон — это плохой сон, а не болезнь. Всё, что
  похоже на медицину, — к врачу, не к тебе.

ЧЕСТНОСТЬ С ДАННЫМИ — прямая просьба Кати, действует всегда:
- не выдавай догадку за факт. Кольцо ИЗМЕРЯЕТ пульс, движение и
  температуру. Стадии сна, «стресс» и готовность оно СЧИТАЕТ алгоритмом —
  это оценка, а не измерение. Разделяй это словами, когда говоришь.
- стадии сна (глубокая, быстрая) — самое слабое место у любых колец.
  Отличить сон от бодрствования они умеют заметно лучше, чем разложить
  его на фазы. Поэтому глубокую фазу смотри трендом за недели, а не
  числом за одну ночь, и не строй на одной ночи выводов.
- «у тебя стресс, отдохни» без её цифр и без основания — запрещено.
  Рекомендации только из двух источников: её собственные данные и то, что
  действительно доказано. Не знаешь — скажи «не знаю».
- её ощущения важнее показаний. Цифры говорят «всё хорошо», а Катя
  чувствует, что разваливается, — права она. Верь ей, а не кольцу.

Главное, ради чего это подключено: у Кати цикл перегруза — несколько дней
на полных оборотах, потом обвал. Она сама просила замечать разгон раньше
неё и говорить прямо, конкретными цифрами, а не общим «побереги себя».
"""


# ── Разговор с Oura ──────────────────────────────────────────────────────

def _get(path: str, start: date, end: date) -> list[dict]:
    """Забрать один раздел за период. Пустой список — если нет данных или доступа."""
    token = get_access_token()
    if not token:
        logger.warning("кольцо не авторизовано — нечего забирать")
        return []
    try:
        resp = httpx.get(
            f"{API}/{path}",
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception:
        logger.exception("не удалось забрать %s из Oura", path)
        return []


def _minutes(seconds) -> int | None:
    return round(seconds / 60) if isinstance(seconds, (int, float)) else None


def _by_day(rows: list[dict]) -> dict[str, dict]:
    return {r["day"]: r for r in rows if r.get("day")}


def _main_sleep(rows: list[dict]) -> dict[str, dict]:
    """Из всех записей сна за день оставить самую длинную — это ночь, а не дрёма."""
    best: dict[str, dict] = {}
    for r in rows:
        day = r.get("day")
        if not day:
            continue
        if day not in best or (r.get("total_sleep_duration") or 0) > (
                best[day].get("total_sleep_duration") or 0):
            best[day] = r
    return best


def collect(days: int = 3) -> int:
    """Забрать последние дни из Oura и сложить в базу. Вернуть, сколько дней записано.

    Берём с запасом: Oura досчитывает ночь не мгновенно, и вчерашняя запись
    может появиться позже. Повторная запись того же дня безопасна — upsert.
    """
    end = date.today()
    start = end - timedelta(days=days)

    sleep = _main_sleep(_get("sleep", start, end))
    daily_sleep = _by_day(_get("daily_sleep", start, end))
    readiness = _by_day(_get("daily_readiness", start, end))
    stress = _by_day(_get("daily_stress", start, end))

    all_days = sorted(set(sleep) | set(daily_sleep) | set(readiness) | set(stress))
    written = 0
    for day in all_days:
        s = sleep.get(day, {})
        row = {
            "day": day,
            "sleep_score": daily_sleep.get(day, {}).get("score"),
            "readiness_score": readiness.get(day, {}).get("score"),
            "temp_deviation": readiness.get(day, {}).get("temperature_deviation"),
            "rhr": s.get("lowest_heart_rate"),
            "avg_hr": s.get("average_heart_rate"),
            "hrv": s.get("average_hrv"),
            "total_min": _minutes(s.get("total_sleep_duration")),
            "deep_min": _minutes(s.get("deep_sleep_duration")),
            "rem_min": _minutes(s.get("rem_sleep_duration")),
            "light_min": _minutes(s.get("light_sleep_duration")),
            "awake_min": _minutes(s.get("awake_time")),
            "latency_min": _minutes(s.get("latency")),
            "efficiency": s.get("efficiency"),
            "restless": s.get("restless_periods"),
            "stress_high_min": _minutes(stress.get(day, {}).get("stress_high")),
            "recovery_high_min": _minutes(stress.get(day, {}).get("recovery_high")),
            "stress_summary": stress.get(day, {}).get("day_summary"),
        }
        try:
            supabase.table("oura_daily").upsert(row, on_conflict="day").execute()
            written += 1
        except Exception:
            logger.exception("не удалось записать день %s", day)
    logger.info("кольцо: записано дней — %d", written)
    return written


# ── Инструменты для Анджелины ────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_oura_day",
        "description": (
            "Данные кольца за один день: сон (глубокая, быстрая фаза, пробуждения), "
            "пульс покоя, HRV, температура, стресс. Без даты — последний день с данными."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD (по умолчанию последний доступный)"},
            },
        },
    },
    {
        "name": "get_oura_trend",
        "description": (
            "Норма Кати и отклонение от неё: средние за период и насколько последний "
            "день от них отличается. Возвращает days_of_data — сколько дней реально "
            "накоплено. Пока их мало, про отклонения говорить нельзя."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "За сколько дней считать норму (по умолчанию 14)"},
            },
        },
    },
]


def _rows(limit: int) -> list[dict]:
    return (
        supabase.table("oura_daily")
        .select("*")
        .order("day", desc=True)
        .limit(limit)
        .execute()
        .data
    )


# База пустая ровно один раз — сразу после первой авторизации. Тянуть
# историю на каждый вопрос незачем, поэтому пробуем один раз за запуск.
_backfilled = False


def _ensure_data() -> None:
    """Если в базе пусто — сходить за историей, а не отвечать «данных нет».

    Нужно из-за порядка событий: сбор стоит на старте бота и на утро, а
    кольцо авторизуется позже. Без этого первый вопрос Кати упирался бы
    в пустую таблицу до следующего утра.
    """
    global _backfilled
    if _backfilled:
        return
    _backfilled = True
    try:
        if not _rows(1):
            collect(60)
    except Exception:
        logger.exception("не удалось подтянуть историю кольца")


def has_data() -> bool:
    """Есть ли в базе хоть один день кольца (нужно планировщику на старте)."""
    try:
        return bool(_rows(1))
    except Exception:
        logger.exception("не смогла проверить, есть ли данные кольца")
        return True  # сомневаешься — не тяни глубокую историю лишний раз


def _days_of_data() -> int:
    """Сколько дней кольца накоплено (больше CALIBRATION_DAYS не считаем)."""
    try:
        return len(_rows(CALIBRATION_DAYS + 1))
    except Exception:
        logger.exception("не смогла посчитать дни кольца")
        return 0


def _with_calibration(row: dict) -> dict:
    """Добавить к данным дня отметку о калибровке.

    Зачем: правило «пока нет нормы — не оценивать» стояло только в промпте
    и опиралось на признак, который приходил ТОЛЬКО из get_oura_trend.
    Спросили день — признака не было, и Анджелина выдала «хорошие цифры»
    на четвёртый день ношения. Тот же класс ошибки, что 3 сентября с
    пометкой [голосовое]: правило лежало верно, а условие до модели не
    доезжало. Поэтому признак теперь едет с КАЖДЫМ ответом.
    """
    days = _days_of_data()
    out = dict(row)
    out["days_of_data"] = days
    out["calibrating"] = days < CALIBRATION_DAYS
    if out["calibrating"]:
        out["note"] = (
            f"Идёт калибровка: накоплено {days} дн. из {CALIBRATION_DAYS}. "
            "Нормы Кати ещё нет. Называть факты можно, оценивать "
            "(«хорошо», «плохо», «выше нормы») — нельзя."
        )
    return out


def _get_oura_day(data: dict) -> dict | None:
    _ensure_data()
    day = data.get("date")
    if day:
        rows = supabase.table("oura_daily").select("*").eq("day", day).limit(1).execute().data
    else:
        rows = _rows(1)
    return _with_calibration(rows[0]) if rows else None


def _average(rows: list[dict], field: str) -> float | None:
    values = [r[field] for r in rows if r.get(field) is not None]
    return round(sum(values) / len(values), 1) if values else None


def _get_oura_trend(data: dict) -> dict:
    _ensure_data()
    days = int(data.get("days") or CALIBRATION_DAYS)
    rows = _rows(days + 1)
    if not rows:
        return {"days_of_data": 0, "calibrating": True,
                "note": "данных кольца в базе ещё нет"}

    latest, earlier = rows[0], rows[1:]
    fields = ("rhr", "hrv", "sleep_score", "readiness_score", "total_min",
              "deep_min", "rem_min", "awake_min", "efficiency")

    baseline = {f: _average(earlier, f) for f in fields}
    deviation = {
        f: round(latest[f] - baseline[f], 1)
        for f in fields
        if latest.get(f) is not None and baseline.get(f) is not None
    }

    days_of_data = len(rows)
    return {
        "days_of_data": days_of_data,
        "calibrating": days_of_data < CALIBRATION_DAYS,
        "calibration_days_needed": max(CALIBRATION_DAYS - days_of_data, 0),
        "latest_day": latest.get("day"),
        "latest": {f: latest.get(f) for f in fields},
        "baseline": baseline,
        "deviation": deviation,
    }


HANDLERS = {
    "get_oura_day": _get_oura_day,
    "get_oura_trend": _get_oura_trend,
}
