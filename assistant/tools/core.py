"""Базовые инструменты — рефлексии (утро/вечер). Всегда включены.

Это образец того, как устроен инструмент:
  • схема в TOOLS — что ассистент может вызвать и с какими полями
  • функция-обработчик — что реально происходит
  • HANDLERS — связывает имя из схемы с функцией
"""

import logging
from datetime import date

from assistant import db
from assistant.formatting import fix_latin_in_russian

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "save_reflection",
        "description": (
            "Сохранить утренний или вечерний шеринг. "
            "Утро: priorities (задачи из «Проф деятельности», минимум три), "
            "main_focus (намерения одной строкой), energy, mood. "
            "Вечер: win (что получилось лучше всего), insight (итог дня), "
            "day_rating, mood. "
            "В notes — полный текст шеринга дословно, как Катя его прислала. "
            "⚠️ ДАТА: по умолчанию ставится СЕГОДНЯШНИЙ день. Катя почти "
            "всегда диктует вечерний шеринг на следующее утро — тогда "
            "обязательно передай date со вчерашним числом, иначе запись "
            "ляжет не в тот день. "
            "Ответ инструмента содержит «дата_в_базе» — это то, что реально "
            "записалось. Сверься с ней, прежде чем говорить «сохранила»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time_of_day": {"type": "string", "enum": ["утро", "вечер"]},
                "main_focus": {"type": "string", "description": "Главный фокус дня (утро)"},
                "priorities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Топ-3 приоритетные задачи (утро)",
                },
                "energy": {"type": "integer", "description": "Энергия 1-10"},
                "mood": {"type": "string", "description": "Настроение"},
                "win": {"type": "string", "description": "Главная победа дня (вечер)"},
                "insight": {"type": "string", "description": "Инсайт дня"},
                "gratitude": {"type": "string", "description": "За что благодарна (вечер)"},
                "day_rating": {"type": "integer", "description": "Оценка дня 1-10 (вечер)"},
                "notes": {"type": "string", "description": "Полный текст ответа"},
                "date": {
                    "type": "string",
                    "description": (
                        "YYYY-MM-DD — день, О КОТОРОМ шеринг, а не день, когда "
                        "его записывают. Не передан — ставится сегодня. "
                        "Шеринг за вчера → передай вчерашнюю дату."
                    ),
                },
            },
            "required": ["time_of_day"],
        },
    },
    {
        "name": "get_reflections",
        "description": (
            "Получить последние рефлексии — для обзоров и вопросов вроде "
            "«какой у меня был фокус сегодня» или «что я планировала»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 7},
                "time_of_day": {"type": "string", "enum": ["утро", "вечер"]},
            },
        },
    },
    {
        "name": "search_history",
        "description": (
            "Поискать по ВСЕЙ прошлой переписке, включая давнюю. "
            "Сама в разговор ты помнишь только последние сообщения — всё, что "
            "старше, доставай этим инструментом. "
            "Вызывай, когда Катя ссылается на прошлое: «мы это обсуждали», "
            "«я тебе говорила про…», «помнишь, я рассказывала», «как звали ту…», "
            "а также когда ищешь свои прежние договорённости и обещания. "
            "query — одно-два ключевых слова, не целая фраза: поиск буквальный. "
            "Ничего не нашлось — попробуй другое слово, потом честно скажи, "
            "что не нашла, и не выдумывай."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Ключевое слово или короткая фраза"},
                "limit": {"type": "integer", "default": 15},
            },
            "required": ["query"],
        },
    },
]


_TEXT_FIELDS = ("main_focus", "mood", "win", "insight", "gratitude", "notes")


def _clean(value):
    """Убрать латиницу из русских слов перед записью в базу.

    Без этого поиск по архиву не находит слово: «dizайнер» в шеринге за
    14 сентября выглядит нормально, но ни Катя, ни Анджелина его уже не
    найдут. Чистим на входе в базу, а не только в исходящем сообщении —
    сюда текст приходит из вызова инструмента, отдельной дорогой.
    """
    if isinstance(value, str):
        return fix_latin_in_russian(value)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _save_reflection(data: dict) -> dict:
    """Сохранить шеринг и ПЕРЕЧИТАТЬ его обратно, прежде чем рапортовать.

    Раньше эта функция всегда возвращала {"saved": True}, не проверяя
    результат и не называя дату. Поэтому 18 сентября два шеринга подряд
    не сохранились, а Анджелина на оба ответила «Записала» — промаха не
    видел никто, и вскрылось это только через три дня, в воскресном
    обзоре. Теперь ответ инструмента — то, что реально лежит в базе.
    """
    ref_date = date.fromisoformat(data["date"]) if data.get("date") else None
    time_of_day = data["time_of_day"]
    fields = {k: _clean(data.get(k)) for k in _TEXT_FIELDS}

    db.save_reflection(
        time_of_day=time_of_day,
        main_focus=fields["main_focus"],
        priorities=_clean(data.get("priorities")),
        energy=data.get("energy"),
        mood=fields["mood"],
        win=fields["win"],
        insight=fields["insight"],
        gratitude=fields["gratitude"],
        day_rating=data.get("day_rating"),
        notes=fields["notes"],
        ref_date=ref_date,
    )

    saved_date = (ref_date or date.today()).isoformat()
    try:
        row = db.get_reflection(saved_date, time_of_day)
    except Exception as exc:
        logger.exception("не смогла перечитать шеринг за %s", saved_date)
        return {
            "saved": "неизвестно",
            "ВАЖНО": (
                "запись отправлена, но проверить её не удалось: "
                f"{exc}. Скажи Кате, что не уверена в сохранении."
            ),
        }

    if not row:
        logger.error("шеринг %s за %s не найден после записи", time_of_day, saved_date)
        return {
            "saved": False,
            "ВАЖНО": (
                "База НЕ подтвердила запись. Не пиши «записала» и не делай "
                "вид, что сохранила — скажи Кате прямо, что шеринг не "
                "сохранился, и предложи прислать ещё раз."
            ),
        }

    filled = [
        k for k in ("main_focus", "priorities", "energy", "mood", "win",
                    "insight", "gratitude", "day_rating", "notes")
        if row.get(k) not in (None, "", [])
    ]
    return {
        "saved": True,
        "дата_в_базе": row.get("date"),
        "time_of_day": row.get("time_of_day"),
        "поля": filled,
        "напоминание": (
            "Это дата, под которой запись реально легла. Если она не та, "
            "о которой шеринг, — вызови save_reflection ещё раз с нужным "
            "date и скажи Кате."
        ),
    }


def _get_reflections(data: dict) -> list[dict]:
    return db.get_reflections(data.get("limit", 7), data.get("time_of_day"))


def _search_history(data: dict) -> list[dict] | dict:
    query = (data.get("query") or "").strip()
    if not query:
        return {"error": "не сказано, что искать"}
    found = db.search_history(query, data.get("limit", 15))
    if not found:
        return {"найдено": 0, "подсказка": f"по слову «{query}» ничего нет — попробуй другое"}
    return found


HANDLERS = {
    "save_reflection": _save_reflection,
    "get_reflections": _get_reflections,
    "search_history": _search_history,
}
