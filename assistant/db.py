"""Supabase — память диалога, рефлексии и журнал чекинов.

Таблицы создаются один раз скриптом schema.sql (см. docs/01-setup.md).
Опциональные модули (цикл, напоминания, Google) используют тот же клиент
`supabase`, импортируя его отсюда.
"""

from datetime import date, datetime, timezone

from supabase import create_client

from assistant.config import SUPABASE_URL, SUPABASE_KEY, TIMEZONE

# Общий клиент. Опциональные модули делают: from assistant.db import supabase
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Память диалога ───────────────────────────────────────────────────
def save_message(role: str, content: str, session_id: str = "default") -> None:
    """Сохранить одно сообщение. role: 'human' или 'ai'."""
    supabase.table("chat_history").insert({
        "session_id": session_id,
        "role": role,
        "content": content,
    }).execute()


_MONTHS_RU = ("янв", "фев", "мар", "апр", "мая", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек")


def _stamp(created_at: str | None) -> str:
    """Отметка времени перед сообщением из истории: «[9 авг, 10:21] ».

    Без неё последние сообщения приходили сплошной стеной, без единого
    признака, когда что было. 10 августа в 7:00 плановый чекин увидел
    хвост воскресного разговора, принял его за сегодняшний и написал
    «мы уже провели утренний чекин» — про катамаран, который был вчера.
    Дату сегодняшнего дня она знала; не знала, какого числа воспоминания.
    """
    if not created_at:
        return ""
    try:
        dt = datetime.fromisoformat(created_at)
    except ValueError:
        return ""
    if dt.tzinfo is None:  # Supabase отдаёт UTC; на всякий случай проставим
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(TIMEZONE)
    return f"[{dt.day} {_MONTHS_RU[dt.month - 1]}, {dt.strftime('%H:%M')}] "


def get_recent_memory(limit: int = 10, session_id: str = "default") -> list[dict]:
    """Последние сообщения в формате Claude (старые → новые), каждое с датой."""
    rows = (
        supabase.table("chat_history")
        .select("role, content, created_at")
        .eq("session_id", session_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
    )
    messages = []
    for row in reversed(rows):  # перевернуть: нужен порядок старые → новые
        content = row.get("content")
        if not content:
            continue
        role = "user" if row.get("role") == "human" else "assistant"
        messages.append({"role": role, "content": _stamp(row.get("created_at")) + content})
    return messages


# ── Рефлексии (утро / вечер) ─────────────────────────────────────────
def save_reflection(
    time_of_day: str,
    main_focus: str | None = None,
    priorities: list[str] | None = None,
    energy: int | None = None,
    mood: str | None = None,
    win: str | None = None,
    insight: str | None = None,
    gratitude: str | None = None,
    day_rating: int | None = None,
    notes: str | None = None,
    ref_date: date | None = None,
) -> dict:
    """Записать утреннюю или вечернюю рефлексию.

    Одна запись на (дата, время суток) — повторный вызов обновляет её,
    а не плодит дубликаты.
    """
    data: dict = {
        "date": (ref_date or date.today()).isoformat(),
        "time_of_day": time_of_day,
    }
    for key, value in {
        "main_focus": main_focus,
        "priorities": priorities,
        "energy": energy,
        "mood": mood,
        "win": win,
        "insight": insight,
        "gratitude": gratitude,
        "day_rating": day_rating,
        "notes": notes,
    }.items():
        if value is not None:
            data[key] = value
    return supabase.table("reflections").upsert(
        data, on_conflict="date,time_of_day"
    ).execute().data


def search_history(query: str, limit: int = 15, session_id: str = "default") -> list[dict]:
    """Найти в ЛЮБОЙ старой переписке сообщения со словом или фразой.

    В разговор автоматически подтягиваются только последние сообщения
    (get_recent_memory). Всё, что старше, лежит в базе, но достать его было
    нечем. Это и есть недостающий ключ: полнотекстовый поиск по архиву.
    """
    rows = (
        supabase.table("chat_history")
        .select("role, content, created_at")
        .eq("session_id", session_id)
        .ilike("content", f"%{query}%")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
    )
    found = []
    for row in reversed(rows):  # старые → новые, так читается естественнее
        content = row.get("content")
        if not content:
            continue
        found.append({
            "когда": (row.get("created_at") or "")[:16].replace("T", " "),
            "кто": "Катя" if row.get("role") == "human" else "я",
            "текст": content,
        })
    return found


def get_reflections(limit: int = 7, time_of_day: str | None = None) -> list[dict]:
    """Последние рефлексии (для обзоров и вопросов «что я планировала»)."""
    q = supabase.table("reflections").select("*")
    if time_of_day:
        q = q.eq("time_of_day", time_of_day)
    return q.order("date", desc=True).limit(limit).execute().data


def has_reflection_today(time_of_day: str, date_str: str) -> bool:
    """Прислала ли Катя шеринг за этот день сама, до планового чекина.

    Она часто пишет раньше расписания — в 6 утра или в 9 вечера. Плановый
    чекин про это не знал и спрашивал то же самое второй раз.
    """
    rows = (
        supabase.table("reflections")
        .select("id")
        .eq("time_of_day", time_of_day)
        .eq("date", date_str)
        .limit(1)
        .execute()
        .data
    )
    return len(rows) > 0


# ── Журнал чекинов (чтобы не отправлять дважды после рестарта) ────────
def was_checkin_sent(label: str, date_str: str) -> bool:
    rows = (
        supabase.table("checkin_log")
        .select("id")
        .eq("label", label)
        .eq("date", date_str)
        .limit(1)
        .execute()
        .data
    )
    return len(rows) > 0


def mark_checkin_sent(label: str, date_str: str) -> None:
    supabase.table("checkin_log").insert({"label": label, "date": date_str}).execute()
